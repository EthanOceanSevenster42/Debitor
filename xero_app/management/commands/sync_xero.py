"""Pull open ACCREC invoices from Xero into the local snapshot table, and enrich
each one with its project code (Tracking category), last-reminder-email date, and
the debtor's full contact details — so the app reads everything from SQL Server
and only the sync ever talks to Xero.

Run manually:  python manage.py sync_xero
On a schedule: Windows Task Scheduler -> python manage.py sync_xero  (hourly)

Rate limits: we run at HALF of Xero's published limits on purpose.
  * 30 calls/min (Xero allows 60) — paced centrally in xero_client._get().
  * 1 call in flight (Xero allows 5) — the sync is sequential.
  * 2,400 calls/day (Xero allows 5,000) — DAILY_API_BUDGET below, summed across
    every run in the calendar day. When the budget is spent the run stops cleanly
    and the next run resumes the remaining enrichment.
"""
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal

import json

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from xero_app.models import (XeroConnection, OpenInvoiceSnapshot, Invoice, SyncRun,
                             ContactDetail, InvoiceHistory, OnlineInvoiceLink, SyncSchedule)
from xero_app.xero_client import (fetch_open_invoices, iter_ar_invoices, fetch_contact,
                                  fetch_invoice_history, fetch_online_invoice_url,
                                  clean_contact, extract_project_code, pacer,
                                  XeroDailyLimitError, XeroCallBudgetReached)
from xero_app.views import _xero_hist_date, _is_email_event
from xero_app import recovery
from xero_app import portfolio

# The full AR-invoice-history backfill (Invoice table) is disabled: the Debtors
# listing that used it was removed, and the Debtors Action Page runs off the
# open-invoice snapshot below. Flip to True to re-enable the full backfill.
SYNC_FULL_INVOICE_HISTORY = False

# Half of Xero's 5,000/day cap, with margin. Summed across all of the day's runs.
DAILY_API_BUDGET = 2400
# Cap per single run so an hourly-scheduled backfill does bounded ~10-min chunks
# that finish well before the next trigger (no overlapping sync processes), while
# the DAILY_API_BUDGET still bounds the whole day. ~300 calls * 2s pace ~= 10 min.
PER_RUN_CALL_CAP = 300
# How long cached enrichment stays fresh before the sync refreshes it (best effort,
# within whatever budget remains after the essential open-invoice refresh).
HISTORY_TTL_DAYS = 7      # reminders accrue on overdue invoices, so refresh weekly
CONTACT_TTL_DAYS = 30     # contact info changes rarely

AGING_BUCKETS = [
    ("0-30", 0, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("91-120", 91, 120),
    ("120+", 121, None),
]


def _parse_datetime(value):
    """Parse a Xero date/datetime string into a tz-aware datetime (or None)."""
    if not value:
        return None
    if isinstance(value, str) and value.startswith("/Date("):
        try:
            ms = int(value[6:-2].split("+")[0].split("-")[0])
            return datetime.fromtimestamp(ms / 1000, tz=dt_tz.utc)
        except (ValueError, IndexError):
            return None
    try:
        dt = datetime.fromisoformat(value[:19])
        return timezone.make_aware(dt, dt_tz.utc) if timezone.is_naive(dt) else dt
    except (ValueError, TypeError):
        return None


def _parse_date(value):
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def _parse_event_dt(value):
    """Parse a cached history event 'date' ('YYYY-MM-DD HH:MM', UTC) -> aware dt."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt_tz.utc)
    except (ValueError, TypeError):
        return None


def _last_reminder(events):
    """From an invoice's cached lifecycle events, return (datetime, stage_label) of
    the most recent reminder email Xero sent, or (None, '')."""
    email_events = [e for e in events if e.get("is_email")]
    # Prefer events that are explicitly reminders; fall back to any email event
    # (e.g. the original "Invoice emailed") if no reminder is recorded yet.
    reminders = [e for e in email_events if "reminder" in (e.get("details") or "").lower()]
    pool = reminders or email_events
    best_dt, best_stage = None, ""
    for e in pool:
        dt = _parse_event_dt(e.get("date"))
        if dt and (best_dt is None or dt > best_dt):
            best_dt, best_stage = dt, (e.get("action") or "")[:64]
    return best_dt, best_stage


def _bucket_for(days_past_due):
    if days_past_due < 0:
        return "Not Yet Due"
    for label, lo, hi in AGING_BUCKETS:
        if hi is None and days_past_due >= lo:
            return label
        if hi is not None and lo <= days_past_due <= hi:
            return label
    return "120+"


class Command(BaseCommand):
    help = "Sync open ACCREC invoices from Xero into the local snapshot table, with enrichment"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only sync this tenant_id", default=None)
        parser.add_argument("--no-enrich", action="store_true",
                            help="Skip contact/history gap-fill (only refresh open invoices + project codes)")
        parser.add_argument("--budget", type=int, default=None,
                            help=f"Override today's remaining call budget (default: {DAILY_API_BUDGET}/day)")
        parser.add_argument("--force", action="store_true",
                            help="Run now, ignoring the configured schedule (used by the Refresh now button)")

    def handle(self, *args, **opts):
        # Honour the user-configured schedule. The Refresh now button passes
        # --force; the Windows Scheduled Task does not, so it skips out-of-window
        # firings cleanly without doing any Xero work.
        if not opts.get("force"):
            sched = SyncSchedule.objects.first()
            if sched:
                last_success = (SyncRun.objects.filter(status=SyncRun.SUCCESS)
                                .order_by("-finished_at").first())
                reason = sched.should_skip(last_success=last_success)
                if reason:
                    self.stdout.write(self.style.WARNING(f"sync_xero skipped: {reason}"))
                    return

        # Clear out any runs left hanging by an interrupted/killed sync so the
        # dashboard status and the incremental watermark stay accurate.
        stale = SyncRun.objects.filter(status=SyncRun.RUNNING)
        if stale.exists():
            stale.update(status=SyncRun.FAILED, error_message="interrupted",
                         finished_at=timezone.now())

        qs = XeroConnection.objects.all()
        if opts.get("tenant"):
            qs = qs.filter(tenant_id=opts["tenant"])
        if not qs.exists():
            self.stderr.write("No XeroConnection rows. Authenticate via /xero/login/ first.")
            return
        for connection in qs:
            self._sync_one(connection, enrich=not opts["no_enrich"],
                           budget_override=opts["budget"])

    def _remaining_budget(self, override):
        if override is not None:
            return max(0, override)
        today = timezone.now().date()
        used = (SyncRun.objects.filter(started_at__date=today)
                .aggregate(n=Sum("api_calls"))["n"] or 0)
        # Bound the whole day, and bound this single run so hourly chunks don't overlap.
        return min(max(0, DAILY_API_BUDGET - used), PER_RUN_CALL_CAP)

    def _sync_one(self, connection, enrich=True, budget_override=None):
        run = SyncRun.objects.create(tenant_id=connection.tenant_id, status=SyncRun.RUNNING)
        budget = self._remaining_budget(budget_override)
        pacer.reset(max_calls=budget)
        self.stdout.write(f"[{connection.tenant_name}] syncing... (call budget this run: {budget})")
        try:
            # 1) Open invoices (full detail -> carries line-item Tracking for project code).
            invoices = fetch_open_invoices(connection)

            # 2) Enrich: fill any missing/stale contact details and invoice histories,
            #    capped by the remaining budget. Done before building snapshots so the
            #    last-reminder dates reflect freshly fetched history.
            if enrich:
                self._enrich(connection, invoices)

            # 3) Build the open-invoice snapshot, attaching project code + last reminder.
            #    Capture the previous open set first so we can detect payments
            #    (a drop in amount_due) and credit admin recoveries afterwards.
            prev_snapshot = recovery.capture_open_snapshot(connection.tenant_id)
            count = self._rebuild_snapshot(connection, invoices)
            try:
                n = recovery.detect_recoveries(connection.tenant_id, prev_snapshot)
                if n:
                    self.stdout.write(f"[{connection.tenant_name}] {n} payment(s) recorded for recovery tracking")
            except Exception as e:  # recovery tracking must never break the sync
                self.stderr.write(self.style.WARNING(f"[{connection.tenant_name}] recovery detection skipped: {e}"))
            # Record where the book stands today (replacing any earlier capture
            # today), so period reports have an opening and closing balance.
            try:
                portfolio.capture_snapshot(connection.tenant_id)
            except Exception as e:  # reporting history must never break the sync
                self.stderr.write(self.style.WARNING(f"[{connection.tenant_name}] portfolio snapshot skipped: {e}"))

            if SYNC_FULL_INVOICE_HISTORY:
                invoice_total = self._sync_invoices(connection)
                msg = (f"[{connection.tenant_name}] {count} open invoices, "
                       f"{invoice_total} AR invoices stored")
            else:
                msg = f"[{connection.tenant_name}] {count} open invoices stored"

            run.invoice_count = count
            run.status = SyncRun.SUCCESS
            self.stdout.write(self.style.SUCCESS(f"{msg} ({pacer.calls} Xero calls)"))
        except XeroCallBudgetReached as e:
            # Hit our half-of-daily budget. Whatever was fetched/built so far is kept;
            # the rest resumes next run. Not a failure.
            run.status = SyncRun.SUCCESS
            run.error_message = str(e)[:500]
            self.stdout.write(self.style.WARNING(f"[{connection.tenant_name}] budget reached: {e}"))
        except XeroDailyLimitError as e:
            # Xero's own cap (shouldn't happen at half speed, but handle it): stop cleanly.
            run.status = SyncRun.FAILED
            run.error_message = str(e)[:500]
            self.stdout.write(self.style.WARNING(f"[{connection.tenant_name}] paused: {e}"))
        except Exception as e:
            run.status = SyncRun.FAILED
            run.error_message = str(e)[:500]
            self.stderr.write(self.style.ERROR(f"[{connection.tenant_name}] FAILED: {e}"))
            raise
        finally:
            run.api_calls = pacer.calls
            run.finished_at = timezone.now()
            run.save()

    # --- enrichment -----------------------------------------------------------

    def _enrich(self, connection, invoices):
        """Fill gaps in ContactDetail and InvoiceHistory for the current open
        invoices, prioritising missing data over stale data. Budget-capped: when
        the per-run budget is exhausted the pacer raises XeroCallBudgetReached,
        which the caller treats as a clean pause."""
        tid = connection.tenant_id
        now = timezone.now()

        open_ids = [inv.get("InvoiceID") for inv in invoices if inv.get("InvoiceID")]
        contact_ids = {(inv.get("Contact") or {}).get("ContactID")
                       for inv in invoices if (inv.get("Contact") or {}).get("ContactID")}

        hist_fetched = dict(InvoiceHistory.objects.filter(tenant_id=tid, invoice_id__in=open_ids)
                            .values_list("invoice_id", "fetched_at"))
        contact_fetched = dict(ContactDetail.objects.filter(tenant_id=tid, contact_id__in=contact_ids)
                               .values_list("contact_id", "fetched_at"))
        have_links = set(OnlineInvoiceLink.objects.filter(tenant_id=tid, invoice_id__in=open_ids)
                         .values_list("invoice_id", flat=True))

        hist_cutoff = now - timedelta(days=HISTORY_TTL_DAYS)
        contact_cutoff = now - timedelta(days=CONTACT_TTL_DAYS)

        missing_hist = [i for i in open_ids if i not in hist_fetched]
        missing_contacts = [c for c in contact_ids if c not in contact_fetched]
        # Online-invoice permalinks never change, so only ever fill the missing ones.
        missing_links = [i for i in open_ids if i not in have_links]
        stale_hist = [i for i, ts in hist_fetched.items() if ts and ts < hist_cutoff]
        stale_contacts = [c for c, ts in contact_fetched.items() if ts and ts < contact_cutoff]

        plan = (f"[{connection.tenant_name}] enrich: "
                f"history {len(missing_hist)} missing / {len(stale_hist)} stale, "
                f"contacts {len(missing_contacts)} missing / {len(stale_contacts)} stale, "
                f"online links {len(missing_links)} missing")
        self.stdout.write(plan)

        # Priority order: actionable data first (reminders), then contacts, then the
        # one-time online-link backfill; missing before stale. The pacer stops us the
        # moment the budget is spent (remaining work resumes next run).
        try:
            for iid in missing_hist:
                self._fetch_history(connection, iid)
            for cid in missing_contacts:
                self._fetch_contact(connection, cid)
            for iid in missing_links:
                self._fetch_online_link(connection, iid)
            for iid in stale_hist:
                self._fetch_history(connection, iid)
            for cid in stale_contacts:
                self._fetch_contact(connection, cid)
        except XeroCallBudgetReached:
            self.stdout.write(self.style.WARNING(
                f"[{connection.tenant_name}] enrichment paused at budget; resumes next run."))
        except XeroDailyLimitError:
            self.stdout.write(self.style.WARNING(
                f"[{connection.tenant_name}] Xero daily limit hit during enrichment; resumes later."))

    def _fetch_history(self, connection, invoice_id):
        records = fetch_invoice_history(connection, invoice_id)
        events = [{
            "date": _xero_hist_date(rec),
            "user": rec.get("User") or "",
            "action": rec.get("Changes") or "",
            "details": rec.get("Details") or "",
            "is_email": _is_email_event(rec.get("Changes") or "", rec.get("Details") or ""),
        } for rec in records]
        InvoiceHistory.objects.update_or_create(
            tenant_id=connection.tenant_id, invoice_id=invoice_id,
            defaults={"events_json": json.dumps(events)},
        )

    def _fetch_contact(self, connection, contact_id):
        raw = fetch_contact(connection, contact_id)
        if not raw:
            return
        ContactDetail.objects.update_or_create(
            tenant_id=connection.tenant_id, contact_id=contact_id,
            defaults={"data_json": json.dumps(clean_contact(raw))},
        )

    def _fetch_online_link(self, connection, invoice_id):
        url = fetch_online_invoice_url(connection, invoice_id)
        if not url:
            return
        OnlineInvoiceLink.objects.update_or_create(
            tenant_id=connection.tenant_id, invoice_id=invoice_id,
            defaults={"url": url},
        )

    # --- snapshot -------------------------------------------------------------

    def _rebuild_snapshot(self, connection, invoices):
        tid = connection.tenant_id
        today = date.today()

        # Last-reminder dates come from the (now freshly enriched) cached history.
        reminder_map = {}
        for iid, events_json in InvoiceHistory.objects.filter(tenant_id=tid).values_list(
                "invoice_id", "events_json"):
            dt, stage = _last_reminder(json.loads(events_json or "[]"))
            if dt:
                reminder_map[iid] = (dt, stage)

        # 'View online' permalinks come from the durable cache (enriched above).
        link_map = dict(OnlineInvoiceLink.objects.filter(tenant_id=tid)
                        .values_list("invoice_id", "url"))

        # Dedupe by invoice_id. Xero's paginated /Invoices endpoint can re-serve
        # the same row across page boundaries when an invoice is edited (or a new
        # one is created) mid-fetch — the ordering shifts and a row at the page
        # boundary appears in two consecutive pages. Without this, bulk_create
        # later hits the (tenant_id, invoice_id) unique constraint and the whole
        # snapshot rebuild rolls back.
        snapshots = {}
        for inv in invoices:
            try:
                amount_due = Decimal(str(inv.get("AmountDue") or 0))
            except (TypeError, ValueError):
                amount_due = Decimal(0)
            if amount_due <= 0:
                continue
            if inv.get("Status") in ("PAID", "VOIDED", "DELETED"):
                continue
            if (inv.get("Type") or "ACCREC") != "ACCREC":
                continue
            iid = inv.get("InvoiceID", "") or ""
            if not iid:
                continue

            due = _parse_date(inv.get("DueDateString") or inv.get("DueDate"))
            days_past_due = (today - due).days if due else 0
            bucket = _bucket_for(days_past_due)
            contact = inv.get("Contact") or {}
            reminder_dt, reminder_stage = reminder_map.get(iid, (None, ""))

            snapshots[iid] = OpenInvoiceSnapshot(
                tenant_id=tid,
                invoice_id=iid,
                invoice_number=inv.get("InvoiceNumber", "") or "",
                contact_id=contact.get("ContactID", "") or "",
                contact_name=contact.get("Name", "") or "",
                contact_email=contact.get("EmailAddress", "") or "",
                invoice_date=_parse_date(inv.get("DateString") or inv.get("Date")),
                due_date=due,
                days_past_due=days_past_due,
                bucket=bucket,
                amount_due=amount_due,
                total=Decimal(str(inv.get("Total") or 0)),
                currency=inv.get("CurrencyCode", "") or "",
                status=inv.get("Status", "") or "",
                project_code=extract_project_code(inv),
                inspector=extract_project_code(inv, category_name="Inspector"),
                online_url=link_map.get(iid, ""),
                last_reminder_at=reminder_dt,
                last_reminder_stage=reminder_stage,
            )

        rows = list(snapshots.values())
        with transaction.atomic():
            OpenInvoiceSnapshot.objects.filter(tenant_id=tid).delete()
            OpenInvoiceSnapshot.objects.bulk_create(rows, batch_size=500)
        return len(rows)

    # --- full AR invoice backfill (disabled by default) -----------------------

    def _invoice_fields(self, connection, inv):
        contact = inv.get("Contact") or {}

        def _dec(key):
            try:
                return Decimal(str(inv.get(key) or 0))
            except (TypeError, ValueError):
                return Decimal(0)

        return {
            "tenant_id": connection.tenant_id,
            "invoice_id": inv.get("InvoiceID", "") or "",
            "invoice_number": inv.get("InvoiceNumber", "") or "",
            "contact_id": contact.get("ContactID", "") or "",
            "contact_name": contact.get("Name", "") or "",
            "contact_email": contact.get("EmailAddress", "") or "",
            "invoice_date": _parse_date(inv.get("DateString") or inv.get("Date")),
            "due_date": _parse_date(inv.get("DueDateString") or inv.get("DueDate")),
            "total": _dec("Total"),
            "amount_due": _dec("AmountDue"),
            "amount_paid": _dec("AmountPaid"),
            "currency": inv.get("CurrencyCode", "") or "",
            "status": inv.get("Status", "") or "",
            "updated_date_utc": _parse_datetime(inv.get("UpdatedDateUTC")),
        }

    def _upsert_invoice_page(self, connection, items):
        """Insert new invoices and update changed ones, in a single transaction."""
        tid = connection.tenant_id
        fields_list = [self._invoice_fields(connection, inv) for inv in items]
        ids = [f["invoice_id"] for f in fields_list if f["invoice_id"]]
        existing = set(Invoice.objects.filter(tenant_id=tid, invoice_id__in=ids)
                       .values_list("invoice_id", flat=True))
        to_create = []
        with transaction.atomic():
            for f in fields_list:
                iid = f["invoice_id"]
                if not iid:
                    continue
                if iid in existing:
                    Invoice.objects.filter(tenant_id=tid, invoice_id=iid).update(
                        **{k: v for k, v in f.items() if k not in ("tenant_id", "invoice_id")}
                    )
                else:
                    to_create.append(Invoice(**f))
            if to_create:
                Invoice.objects.bulk_create(to_create, batch_size=500)

    def _sync_invoices(self, connection):
        """Refresh the local Invoice table for this tenant, streaming page by page."""
        tid = connection.tenant_id
        sync_start = timezone.now()

        if connection.invoices_synced_at:
            # 2h overlap so an invoice changed right before last sync isn't missed.
            modified_since = connection.invoices_synced_at - timedelta(hours=2)
            mode = "incremental"
        else:
            modified_since = None
            mode = "full backfill"

        total_seen = 0
        for page, items in iter_ar_invoices(connection, modified_since=modified_since):
            self._upsert_invoice_page(connection, items)
            total_seen += len(items)
            self.stdout.write(f"[{connection.tenant_name}]   invoices page {page} ({total_seen} processed)")

        connection.invoices_synced_at = sync_start
        connection.save(update_fields=["invoices_synced_at"])

        self.stdout.write(f"[{connection.tenant_name}] invoice sync ({mode}): {total_seen} processed")
        return Invoice.objects.filter(tenant_id=tid).count()
