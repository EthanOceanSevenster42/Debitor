"""Where every open rand sits, who is responsible for it, and what kind of
client owes it.

The Debtors Action, Handover and Lawyers pages each decide for themselves which
invoices they show. Reporting needs one answer for the whole book, so this
module classifies every open, not-written-off invoice into exactly one status,
following the same rules those pages apply:

  * CLOSED    - the debtor is on the Closed Debtors page.
  * LEGAL     - the debtor has an approved (active) matter with the attorneys.
                The whole client leaves the clerk's action page and is monitored
                from the Lawyers page; none of it counts as active workload.
  * HANDOVER  - the invoice is on the Handover page (marked, or aged past the
                debtor's threshold), or the client is, which takes the rest of
                its invoices off the action page with it. An invoice a person
                deliberately took back out of handover stays ACTIVE.
  * ACTIVE    - everything else: the clerk's working book.

It also resolves each debtor's clerk (DebtorAllocation) and category (a hand-set
DebtorClassification, else the first category whose keyword appears in the
debtor's name or project codes), and writes the daily PortfolioSnapshot rows
that period reports compare against.

Pure database work - no Xero calls - so it is safe to run from a page view or
at the end of a sync.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import outreach
from .models import (CallLog, ClosedDebtor, DebtorAllocation, DebtorCategory,
                     DebtorClassification, HandoverInvoice, HandoverReturn,
                     HandoverSetting, LegalMatter, OpenInvoiceSnapshot,
                     PortfolioSnapshot, SystemSetting, WriteOffInvoice,
                     DEFAULT_HANDOVER_DAYS)

ACTIVE = PortfolioSnapshot.STATUS_ACTIVE
HANDOVER = PortfolioSnapshot.STATUS_HANDOVER
LEGAL = PortfolioSnapshot.STATUS_LEGAL
CLOSED = PortfolioSnapshot.STATUS_CLOSED
# Where a debtor sits, as the finance team would say it.
STATUS_LABELS = {ACTIVE: "Being chased", HANDOVER: "Awaiting handover",
                 LEGAL: "With attorneys", CLOSED: "Closed"}
# The statuses that make up a clerk's portfolio. Closed debtors are out of it.
OPEN_STATUSES = (ACTIVE, HANDOVER, LEGAL)

BUCKETS = ["Not Yet Due", "0-30", "31-60", "61-90", "91-120", "120+"]
BUCKET_FIELDS = {"Not Yet Due": "not_due", "0-30": "b0_30", "31-60": "b31_60",
                 "61-90": "b61_90", "91-120": "b91_120", "120+": "b120_plus"}

UNCATEGORISED = "Uncategorised"
UNALLOCATED = "No clerk assigned"


# ---- rules shared with the listing pages --------------------------------------

def handover_threshold_map(tenant_id):
    """contact_id -> the days-past-due threshold at which that debtor's invoices
    auto-land on the Handover page, or None for 'never auto-hand over'. Debtors
    with no HandoverSetting row aren't in the map (they use the default)."""
    return {hs["contact_id"]: (hs["handover_days"] if hs["auto_handover"] else None)
            for hs in HandoverSetting.objects.filter(tenant_id=tenant_id).values(
                "contact_id", "auto_handover", "handover_days")}


def is_auto_handover(contact_id, days_past_due, threshold_map):
    threshold = threshold_map.get(contact_id or "", DEFAULT_HANDOVER_DAYS)
    return threshold is not None and (days_past_due or 0) >= threshold


def cadence_shift_map(tenant_id):
    """contact_id -> days to push the follow-up cadence later (0 if no override).
    Only debtors with a non-zero shift are in the map."""
    return {hs["contact_id"]: hs["cadence_shift_days"]
            for hs in HandoverSetting.objects.filter(tenant_id=tenant_id)
            .exclude(cadence_shift_days=0).values("contact_id", "cadence_shift_days")}


def effective_dpd(days_past_due, contact_id, shift_map):
    """Days-past-due as the follow-up cadence sees it (see views._effective_dpd)."""
    return (days_past_due or 0) - shift_map.get(contact_id or "", 0)


def channel_due_map(tenant_id):
    """contact_id -> {'call': n|None, 'whatsapp': n|None, 'email': n|None}: the
    per-debtor override of when each follow-up channel becomes due. Only debtors
    with at least one override are in the map."""
    out = {}
    for hs in (HandoverSetting.objects.filter(tenant_id=tenant_id)
               .exclude(call_due_days__isnull=True, whatsapp_due_days__isnull=True,
                        email_due_days__isnull=True)
               .values("contact_id", "call_due_days", "whatsapp_due_days", "email_due_days")):
        out[hs["contact_id"]] = {"call": hs["call_due_days"],
                                 "whatsapp": hs["whatsapp_due_days"],
                                 "email": hs["email_due_days"]}
    return out


CONTACT_CHANNELS = (CallLog.ACTION_CALL, CallLog.ACTION_WHATSAPP, CallLog.ACTION_EMAIL)


def contact_log_sets(tenant_id, since):
    """Per-channel sets of invoice_ids with a logged contact attempt: (recent,
    ever), each {action_type: set(invoice_id)}. 'recent' is on/after `since`."""
    recent = {c: set() for c in CONTACT_CHANNELS}
    ever = {c: set() for c in CONTACT_CHANNELS}
    for invoice_id, action_type, called_at in (
            CallLog.objects.filter(tenant_id=tenant_id)
            .values_list("invoice_id", "action_type", "called_at")):
        # Unknown/legacy values fall back to the Call channel.
        if action_type not in ever:
            action_type = CallLog.ACTION_CALL
        ever[action_type].add(invoice_id)
        if called_at and called_at >= since:
            recent[action_type].add(invoice_id)
    return recent, ever


def missed_suppressed(invoice_date, go_live_date):
    """Invoices issued before go-live never flag as missed."""
    return bool(go_live_date and invoice_date and invoice_date < go_live_date)


def legal_contact_keys(tenant_id, statuses=(LegalMatter.ACTIVE,)):
    """Debtor keys (contact_id and contact_name) of matters in `statuses`. Both
    are kept because a debtor is grouped under its name when it has no id."""
    keys = set()
    for cid, name in (LegalMatter.objects.filter(tenant_id=tenant_id, status__in=statuses)
                      .values_list("contact_id", "contact_name")):
        if cid:
            keys.add(cid)
        if name:
            keys.add(name)
    return keys


# ---- categories -----------------------------------------------------------------

class CategoryResolver:
    """Resolves a debtor to its category: a hand-set classification first, then
    the first active category (in sort order) with a keyword found in the
    debtor's name or project codes. Loaded once, used for the whole book."""

    def __init__(self, tenant_id):
        self.all = list(DebtorCategory.objects.all())
        self.by_id = {c.id: c for c in self.all}
        self.active = [c for c in self.all if c.is_active]
        self._keyworded = [(c, c.keyword_list) for c in self.active if c.keyword_list]
        self.manual = dict(DebtorClassification.objects.filter(tenant_id=tenant_id)
                           .values_list("contact_id", "category_id"))

    def auto_match(self, name="", project_codes=()):
        haystacks = [(name or "").lower()] + [(p or "").lower() for p in project_codes]
        for cat, words in self._keyworded:
            if any(w in h for w in words for h in haystacks):
                return cat
        return None

    def resolve(self, cid, name="", project_codes=()):
        """-> (category_id | None, category_name, source) with source 'manual',
        'auto' or '' (uncategorised)."""
        cat_id = self.manual.get(cid)
        if cat_id and cat_id in self.by_id:
            return cat_id, self.by_id[cat_id].name, "manual"
        cat = self.auto_match(name, project_codes)
        if cat:
            return cat.id, cat.name, "auto"
        return None, UNCATEGORISED, ""


# ---- the book -------------------------------------------------------------------

def _admin_label(user):
    return (user.get_full_name() or user.email) if user else ""


def _codes(project_code):
    return [c.strip() for c in (project_code or "").split(", ") if c.strip()]


def load_book(tenant_id, resolver=None):
    """Classify every open invoice and group them into positions.

    Returns a dict with:
      * ``positions`` - one per (debtor, status): cid, contact_id, name, status,
        admin_id, admin_name, category_id, category_name, category_source,
        invoice_count, buckets {label: Decimal}, total, overdue, max_dpd,
        invoices [invoice dicts].
      * ``invoices``  - every classified invoice (dicts, including ``status``).
      * ``resolver``, ``alloc`` (cid -> User) and ``category_of`` (cid ->
        (id, name, source)) for callers that need to slice other data the same
        way.
    Written-off invoices are left out entirely, as on every listing page.
    """
    resolver = resolver or CategoryResolver(tenant_id)
    closed_ids = set(ClosedDebtor.objects.filter(tenant_id=tenant_id)
                     .values_list("contact_id", flat=True))
    written_off = set(WriteOffInvoice.objects.filter(tenant_id=tenant_id)
                      .values_list("invoice_id", flat=True))
    returned = set(HandoverReturn.objects.filter(tenant_id=tenant_id)
                   .values_list("invoice_id", flat=True))
    handover_rows = set(HandoverInvoice.objects.filter(tenant_id=tenant_id)
                        .values_list("invoice_id", flat=True))
    thresholds = handover_threshold_map(tenant_id)
    legal_keys = legal_contact_keys(tenant_id)
    alloc = {a.contact_id: a.administrator for a in
             DebtorAllocation.objects.filter(tenant_id=tenant_id).select_related("administrator")}

    snaps = list(OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id).values(
        "invoice_id", "invoice_number", "contact_id", "contact_name", "invoice_date",
        "due_date", "days_past_due", "bucket", "amount_due", "project_code"))

    def cid_of(s):
        return s["contact_id"] or s["contact_name"] or "Unknown"

    def on_handover(s):
        return ((s["invoice_id"] in handover_rows
                 or is_auto_handover(s["contact_id"], s["days_past_due"], thresholds))
                and s["invoice_id"] not in returned)

    # A client showing on the Handover page takes its other invoices off the
    # action page with it (mirrors views._aging_context).
    handed_over = {cid_of(s) for s in snaps
                   if s["invoice_id"] not in written_off
                   and cid_of(s) not in closed_ids and cid_of(s) not in legal_keys
                   and on_handover(s)}

    codes_by_cid = defaultdict(set)
    names = {}
    for s in snaps:
        codes_by_cid[cid_of(s)].update(_codes(s["project_code"]))
        names.setdefault(cid_of(s), s["contact_name"] or "Unknown")
    category_of = {cid: resolver.resolve(cid, names[cid], sorted(codes_by_cid[cid]))
                   for cid in names}

    invoices, positions = [], {}
    for s in snaps:
        if s["invoice_id"] in written_off:
            continue
        cid = cid_of(s)
        if cid in closed_ids:
            status = CLOSED
        elif cid in legal_keys:
            status = LEGAL
        elif s["invoice_id"] in returned:
            status = ACTIVE
        elif on_handover(s) or cid in handed_over:
            status = HANDOVER
        else:
            status = ACTIVE
        inv = dict(s, cid=cid, status=status)
        invoices.append(inv)

        pos = positions.get((cid, status))
        if pos is None:
            admin = alloc.get(cid)
            cat_id, cat_name, cat_src = category_of[cid]
            pos = positions[(cid, status)] = {
                "cid": cid, "contact_id": s["contact_id"] or "", "name": names[cid],
                "status": status, "status_label": STATUS_LABELS[status],
                "admin_id": admin.id if admin else None, "admin_name": _admin_label(admin),
                "category_id": cat_id, "category_name": cat_name, "category_source": cat_src,
                "invoice_count": 0, "buckets": {b: Decimal(0) for b in BUCKETS},
                "total": Decimal(0), "overdue": Decimal(0), "max_dpd": 0, "invoices": [],
            }
        amt = s["amount_due"] or Decimal(0)
        pos["invoice_count"] += 1
        pos["buckets"][s["bucket"] if s["bucket"] in pos["buckets"] else "120+"] += amt
        pos["total"] += amt
        if (s["days_past_due"] or 0) > 0:
            pos["overdue"] += amt
        pos["max_dpd"] = max(pos["max_dpd"], s["days_past_due"] or 0)
        pos["invoices"].append(inv)

    return {"positions": list(positions.values()), "invoices": invoices,
            "resolver": resolver, "alloc": alloc, "category_of": category_of}


def invoice_status_map(tenant_id):
    """invoice_id -> status for every open, not-written-off invoice."""
    return {inv["invoice_id"]: inv["status"] for inv in load_book(tenant_id)["invoices"]}


# ---- daily snapshots --------------------------------------------------------------

def capture_snapshot(tenant_id, day=None, book=None):
    """Write today's (or `day`'s) PortfolioSnapshot rows from the current book,
    replacing any already written for that day. Returns the row count."""
    day = day or timezone.localdate()
    book = book or load_book(tenant_id)
    rows = []
    for p in book["positions"]:
        fields = {BUCKET_FIELDS[b]: p["buckets"][b] for b in BUCKETS}
        rows.append(PortfolioSnapshot(
            tenant_id=tenant_id, day=day, contact_id=p["cid"][:255],
            contact_name=p["name"][:255], administrator_id=p["admin_id"],
            administrator_name=p["admin_name"][:255], status=p["status"],
            invoice_count=p["invoice_count"], total=p["total"],
            max_days_past_due=p["max_dpd"], **fields))
    with transaction.atomic():
        PortfolioSnapshot.objects.filter(tenant_id=tenant_id, day=day).delete()
        PortfolioSnapshot.objects.bulk_create(rows, batch_size=500)
    return len(rows)


def ensure_snapshot(tenant_id):
    """Capture today's snapshot if the sync has not already done so."""
    today = timezone.localdate()
    if not PortfolioSnapshot.objects.filter(tenant_id=tenant_id, day=today).exists():
        if OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id).exists():
            capture_snapshot(tenant_id, today)


# ---- follow-up state for the escalation list -----------------------------------

def missed_invoice_ids(tenant_id, invoices):
    """invoice_ids (from `invoices`, dicts carrying contact_id / days_past_due /
    invoice_date) where any follow-up channel is missed - the same test the
    Debtors Action page and dashboard apply."""
    _recent, ever = contact_log_sets(tenant_id, timezone.now() - timedelta(days=7))
    shifts = cadence_shift_map(tenant_id)
    overrides = channel_due_map(tenant_id)
    go_live = SystemSetting.get_solo().go_live_date
    out = set()
    for inv in invoices:
        if missed_suppressed(inv["invoice_date"], go_live):
            continue
        eff = effective_dpd(inv["days_past_due"], inv["contact_id"], shifts)
        ch = overrides.get(inv["contact_id"]) or {}
        iid = inv["invoice_id"]
        if (outreach.channel_missed(eff, iid in ever[CallLog.ACTION_CALL], ch.get("call"))
                or outreach.channel_missed(eff, iid in ever[CallLog.ACTION_WHATSAPP], ch.get("whatsapp"))
                or outreach.channel_missed(eff, iid in ever[CallLog.ACTION_EMAIL], ch.get("email"))):
            out.add(iid)
    return out
