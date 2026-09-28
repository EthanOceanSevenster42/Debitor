"""Performance reports for finance and management.

Answers, for any week / month / quarter / year (or a custom range), optionally
narrowed to one clerk and / or one debtor category:

  * who is responsible for what - accounts and value per clerk, split into
    active collections, handover and with the attorneys;
  * what came in - money received against each portfolio, what counts as the
    clerk's own collection, and the recovery percentage;
  * how the debt moved - opening and closing balance, collections, write-offs,
    the ageing at each end, and who went to the attorneys;
  * what was done - calls, WhatsApps, emails and comments, and how many
    accounts were actually followed up;
  * where to step in - the accounts that need escalation or a decision.

Balances at a past date come from PortfolioSnapshot, which only exists from the
day this feature went live, so a period that starts earlier shows its opening
balance as "not recorded". Everything that happened (payments, write-offs,
actions) comes from the event tables and has its full history.

Attribution:
  * the live portfolio uses the allocation as it stands now;
  * a snapshot uses the allocation on the day it was taken;
  * a payment uses the allocation when the money came in;
  * an action belongs to the person who logged it;
  * a category is always the debtor's current category - categories describe
    what the client is, which does not change when the book is re-organised.
"""
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Max
from django.utils import timezone

from . import portfolio
from .models import (CallLog, DebtorComment, HandoverInvoice, LegalMatter,
                     PortfolioSnapshot, RecoveredInvoice, WriteOffInvoice)
from .portfolio import ACTIVE, HANDOVER, LEGAL, CLOSED, OPEN_STATUSES, BUCKETS, BUCKET_FIELDS

User = get_user_model()

# An overdue account with no call, WhatsApp, email or comment for this long
# needs someone to pick it up.
NO_CONTACT_DAYS = 30
# Days past due at which an account still in active collections is past final
# demand and needs a handover decision.
ESCALATE_DPD = 60
# A matter with the attorneys that has not moved for this long is chased.
LEGAL_IDLE_DAYS = 14

# Ordinal teal ramp for ageing, light (not yet due) to dark (120+ days).
# Validated as one hue, monotone lightness, light end >= 2:1 on the page.
AGEING_COLOURS = ["#74bdb8", "#4aa8a2", "#2a8f89", "#12746e", "#0b5955", "#063f3c"]

PERIOD_KINDS = [("week", "Week"), ("month", "Month"), ("quarter", "Quarter"),
                ("year", "Year"), ("custom", "Custom")]
TREND_LENGTH = {"week": 12, "month": 12, "quarter": 8, "year": 5, "custom": 6}

# Why an account is on the attention list, in the words the finance team uses.
ESCALATION_REASONS = [
    ("missed", "A follow-up was missed"),
    ("aged", f"Over {ESCALATE_DPD} days late, not handed over"),
    ("no_contact", f"No contact for {NO_CONTACT_DAYS} days"),
    ("unallocated", "No clerk assigned"),
    ("handover_decision", "Waiting for a handover decision"),
    ("legal_pending", "Waiting for approval to go to the attorneys"),
    ("legal_idle", f"Attorneys quiet for {LEGAL_IDLE_DAYS}+ days"),
]
ESCALATION_LABELS = dict(ESCALATION_REASONS)
# ...and what somebody should do about it.
ESCALATION_ACTIONS = {
    "missed": "Call, WhatsApp or email the debtor - a reminder was due and nothing was logged",
    "aged": "Decide whether to hand this debtor over",
    "no_contact": "Contact the debtor and log it",
    "unallocated": "Assign a clerk on the Debtors Action page",
    "handover_decision": "Super Admin: send to the attorneys, or bring back",
    "legal_pending": "Super Admin: approve it on the Lawyers page",
    "legal_idle": "Ask the attorneys for an update",
}

# Age bands as a person would say them.
BUCKET_PLAIN = {"Not Yet Due": "Not due yet", "0-30": "0-30 days late",
                "31-60": "31-60 days late", "61-90": "61-90 days late",
                "91-120": "91-120 days late", "120+": "Over 120 days late"}

# RecoveredInvoice.reason -> where the account sat when the money came in.
_REASON_STAGE = {
    RecoveredInvoice.REASON_COLLECTED: ACTIVE,
    RecoveredInvoice.REASON_NO_FOLLOWUP: ACTIVE,
    RecoveredInvoice.REASON_UNALLOCATED: ACTIVE,
    RecoveredInvoice.REASON_HANDOVER_LEGAL: HANDOVER,
    RecoveredInvoice.REASON_COLLECTED_LEGAL: LEGAL,
}


# ---- periods ---------------------------------------------------------------------

def _last_day_of_month(y, m):
    return (date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1))


def _add_months(d, n):
    """The 1st of the month `n` months from `d`'s month."""
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def _whole_months(start, end):
    """How many calendar months start..end covers exactly, or None when the range
    does not begin on a 1st and end on a month's last day."""
    if start.day != 1 or end != _last_day_of_month(end.year, end.month):
        return None
    return (end.year - start.year) * 12 + end.month - start.month + 1


@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date   # inclusive

    @property
    def days(self):
        return (self.end - self.start).days + 1

    @property
    def noun(self):
        return "period" if self.kind == "custom" else self.kind

    @property
    def label(self):
        if self.kind == "week":
            return f"Week of {self.start:%d %b %Y}"
        if self.kind == "month":
            return f"{self.start:%B %Y}"
        if self.kind == "quarter":
            q = (self.start.month - 1) // 3 + 1
            return f"Q{q} {self.start.year} ({self.start:%b}-{self.end:%b})"
        if self.kind == "year":
            return str(self.start.year)
        return self.dates

    @property
    def dates(self):
        """'1 Sep - 30 Sep 2026' - the exact days, for when the label alone is not enough."""
        if self.start.year == self.end.year:
            return f"{self.start.day} {self.start:%b} - {self.end.day} {self.end:%b %Y}"
        return f"{self.start.day} {self.start:%b %Y} - {self.end.day} {self.end:%b %Y}"

    @property
    def short(self):
        if self.kind == "week":
            return f"{self.start:%d %b}"
        if self.kind == "month":
            return f"{self.start:%b '%y}"
        if self.kind == "quarter":
            return f"Q{(self.start.month - 1) // 3 + 1} '{self.start:%y}"
        if self.kind == "year":
            return str(self.start.year)
        # A custom range needs both ends to mean anything on its own.
        return f"{self.start.day} {self.start:%b} - {self.end.day} {self.end:%b '%y}"

    def start_dt(self):
        return timezone.make_aware(datetime.combine(self.start, time.min))

    def end_dt(self):
        """Exclusive: midnight at the start of the day after `end`."""
        return timezone.make_aware(datetime.combine(self.end + timedelta(days=1), time.min))

    def previous(self):
        return self._step(-1)

    def next(self):
        return self._step(1)

    def _step(self, direction):
        if self.kind != "custom":
            edge = self.start - timedelta(days=1) if direction < 0 else self.end + timedelta(days=1)
            return period_for(self.kind, edge)
        # A custom range of whole months steps by months, so "Jan-Dec 2026" goes
        # back to "Jan-Dec 2025" rather than drifting a day in a leap year.
        months = _whole_months(self.start, self.end)
        if months:
            s = _add_months(self.start, direction * months)
            e = _add_months(s, months) - timedelta(days=1)
            return Period("custom", s, e)
        shift = timedelta(days=self.days * direction)
        return Period("custom", self.start + shift, self.end + shift)

    def query(self):
        """The GET parameters that select this period."""
        if self.kind == "custom":
            return {"period": "custom", "from": self.start.isoformat(), "to": self.end.isoformat()}
        return {"period": self.kind, "d": self.start.isoformat()}


def period_for(kind, anchor, start=None, end=None):
    """The period of `kind` containing `anchor` (or start..end for custom)."""
    if kind == "week":
        s = anchor - timedelta(days=anchor.weekday())
        return Period(kind, s, s + timedelta(days=6))
    if kind == "quarter":
        m = 3 * ((anchor.month - 1) // 3) + 1
        return Period(kind, date(anchor.year, m, 1), _last_day_of_month(anchor.year, m + 2))
    if kind == "year":
        return Period(kind, date(anchor.year, 1, 1), date(anchor.year, 12, 31))
    if kind == "custom" and start and end:
        if end < start:
            start, end = end, start
        return Period(kind, start, end)
    return Period("month", anchor.replace(day=1), _last_day_of_month(anchor.year, anchor.month))


def _parse_date(value):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def period_from_request(params, today=None):
    today = today or timezone.localdate()
    kind = (params.get("period") or "month").strip()
    if kind not in dict(PERIOD_KINDS):
        kind = "month"
    if kind == "custom":
        s, e = _parse_date(params.get("from")), _parse_date(params.get("to"))
        if s and e:
            return period_for("custom", today, s, e)
        # An incomplete custom range falls back to month-to-date.
        return period_for("custom", today, today.replace(day=1), today)
    return period_for(kind, _parse_date(params.get("d")) or today)


def trend_periods(period):
    """The run of periods leading up to (and including) `period`, oldest first."""
    out = [period]
    for _ in range(TREND_LENGTH.get(period.kind, 12) - 1):
        out.insert(0, out[0].previous())
    return out


# ---- scope -----------------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    """Which slice of the book a report covers. clerk: None (everyone),
    'unallocated', or a user id. category: None (all), 'none'
    (uncategorised), or a DebtorCategory id."""
    clerk: object = None
    category: object = None

    def clerk_ok(self, admin_id):
        if self.clerk is None:
            return True
        if self.clerk == "unallocated":
            return admin_id is None
        return admin_id == self.clerk

    def category_ok(self, category_id):
        if self.category is None:
            return True
        if self.category == "none":
            return category_id is None
        return category_id == self.category

    def query(self):
        q = {}
        if self.clerk is not None:
            q["clerk"] = str(self.clerk)
        if self.category is not None:
            q["category"] = str(self.category)
        return q


def scope_from_request(params, user, category_ids):
    """Administrators only ever see their own portfolio; Super Admins choose."""
    if user.is_super_admin:
        raw = (params.get("clerk") or "").strip()
        clerk = "unallocated" if raw == "unallocated" else (int(raw) if raw.isdigit() else None)
    else:
        clerk = user.id
    raw = (params.get("category") or "").strip()
    if raw == "none":
        category = "none"
    elif raw.isdigit() and int(raw) in category_ids:
        category = int(raw)
    else:
        category = None
    return Scope(clerk=clerk, category=category)


# ---- small maths -------------------------------------------------------------------

def pct(part, whole):
    part, whole = float(part or 0), float(whole or 0)
    return (part / whole * 100) if whole else None


def abbr_money(v):
    """Compact rand label for chart axes: R 1.2M / R 340k / R 0."""
    v = float(v or 0)
    a = abs(v)
    if a >= 1_000_000:
        return f"R {v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"R {v / 1_000:.0f}k"
    return f"R {v:.0f}"


def nice_ceil(v):
    """Round a max value up to a clean axis top (1/2/5 x 10^k) for tidy gridlines."""
    v = float(v or 0)
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    frac = v / base
    nice = 1 if frac <= 1 else (2 if frac <= 2 else (5 if frac <= 5 else 10))
    return nice * base


def y_ticks(top, plot_top, plot_bottom, n=4):
    """Evenly-spaced horizontal axis ticks from 0 (bottom) to `top`, each with the
    pixel y for the gridline, a text baseline y, and an abbreviated rand label."""
    ticks = []
    for i in range(n + 1):
        frac = i / n
        y = plot_bottom - frac * (plot_bottom - plot_top)
        ticks.append({"y": round(y, 1), "ty": round(y + 3.5, 1),
                      "label": abbr_money(top * frac)})
    return ticks


def _column_path(x, y, w, baseline, r=4.0):
    """A column rounded at its data end only, square on the baseline."""
    h = baseline - y
    if h <= 0:
        return ""
    r = min(r, h, w / 2)
    return (f"M{x:.1f},{baseline:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"L{x + w - r:.1f},{y:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"L{x + w:.1f},{baseline:.1f} Z")


def _chart_frame(values, width, height):
    pad_l, pad_r, pad_t, pad_b = 56, 12, 14, 26
    top = nice_ceil(max(values, default=0)) if any(values) else 1.0
    plot_w = width - pad_l - pad_r
    baseline = pad_t + (height - pad_t - pad_b)
    return top, pad_l, plot_w, pad_t, baseline


def column_chart(points, width=760, height=220):
    """Single-series column chart: points = [(label, short, value)]."""
    values = [float(v or 0) for _, _, v in points]
    top, pad_l, plot_w, plot_top, baseline = _chart_frame(values, width, height)
    n = len(points)
    slot = plot_w / n if n else plot_w
    bar_w = min(24.0, slot * 0.6)
    cols = []
    for i, (label, short, v) in enumerate(points):
        cx = pad_l + slot * i + slot / 2
        y = baseline - (float(v or 0) / top * (baseline - plot_top))
        cols.append({"label": label, "short": short, "value": v, "cx": round(cx, 1),
                     "path": _column_path(cx - bar_w / 2, y, bar_w, baseline),
                     "hit_x": round(pad_l + slot * i, 1), "hit_w": round(slot, 1)})
    return {"cols": cols, "width": width, "height": height, "baseline": round(baseline, 1),
            "plot_left": pad_l, "plot_right": round(width - 12, 1), "ylabel_x": pad_l - 8,
            "label_y": round(baseline + 16, 1), "plot_top": plot_top,
            "hit_h": round(baseline - plot_top, 1),
            "yticks": y_ticks(top, plot_top, baseline),
            "has_data": any(values)}


def stacked_chart(points, width=760, height=240, gap=2.0):
    """Stacked columns by ageing bucket: points = [(label, short, {bucket: v} | None)].
    A None point (no snapshot for that period) leaves an empty slot."""
    totals = [float(sum(b.values())) if b else 0.0 for _, _, b in points]
    top, pad_l, plot_w, plot_top, baseline = _chart_frame(totals, width, height)
    n = len(points)
    slot = plot_w / n if n else plot_w
    bar_w = min(24.0, slot * 0.6)
    scale = (baseline - plot_top) / top
    cols = []
    for i, (label, short, buckets) in enumerate(points):
        cx = pad_l + slot * i + slot / 2
        x = cx - bar_w / 2
        segs, y = [], baseline
        present = [(b, float(buckets.get(b, 0) or 0)) for b in BUCKETS] if buckets else []
        present = [(b, v) for b, v in present if v > 0]
        for j, (b, v) in enumerate(present):
            h = v * scale
            is_top = j == len(present) - 1
            # A 2px surface gap separates touching segments; the topmost one
            # carries the rounded data end.
            seg_top = y - h
            seg_bottom = y - (gap if j else 0)
            if seg_bottom - seg_top <= 0.5:
                y = seg_top
                continue
            if is_top:
                path = _column_path(x, seg_top, bar_w, seg_bottom)
            else:
                path = (f"M{x:.1f},{seg_bottom:.1f} L{x:.1f},{seg_top:.1f} "
                        f"L{x + bar_w:.1f},{seg_top:.1f} L{x + bar_w:.1f},{seg_bottom:.1f} Z")
            segs.append({"bucket": BUCKET_PLAIN[b], "value": v, "path": path,
                         "colour": AGEING_COLOURS[BUCKETS.index(b)]})
            y = seg_top
        cols.append({"label": label, "short": short, "cx": round(cx, 1), "segs": segs,
                     "total": totals[i], "missing": buckets is None,
                     "buckets": [(BUCKET_PLAIN[b], (buckets or {}).get(b, 0)) for b in BUCKETS]})
    return {"cols": cols, "width": width, "height": height, "baseline": round(baseline, 1),
            "plot_left": pad_l, "plot_right": round(width - 12, 1), "ylabel_x": pad_l - 8,
            "label_y": round(baseline + 16, 1), "yticks": y_ticks(top, plot_top, baseline),
            "legend": list(zip([BUCKET_PLAIN[b] for b in BUCKETS], AGEING_COLOURS)),
            "has_data": any(totals)}


# ---- balances -------------------------------------------------------------------------

def _live_rows(book):
    return [{"cid": p["cid"], "name": p["name"], "admin_id": p["admin_id"],
             "status": p["status"], "total": p["total"], "buckets": dict(p["buckets"]),
             "invoice_count": p["invoice_count"]}
            for p in book["positions"]]


def _snapshot_rows(tenant_id, days):
    """{day: [row]} for the given snapshot days."""
    out = defaultdict(list)
    if not days:
        return out
    fields = ["day", "contact_id", "contact_name", "administrator_id", "status",
              "total", "invoice_count"] + list(BUCKET_FIELDS.values())
    for r in PortfolioSnapshot.objects.filter(tenant_id=tenant_id, day__in=days).values(*fields):
        out[r["day"]].append({
            "cid": r["contact_id"], "name": r["contact_name"], "admin_id": r["administrator_id"],
            "status": r["status"], "total": r["total"], "invoice_count": r["invoice_count"],
            "buckets": {b: r[f] for b, f in BUCKET_FIELDS.items()}})
    return out


class _Balances:
    """Balance lookups for a set of periods: the live book for any period that
    reaches today, else the snapshot nearest the date asked for."""

    def __init__(self, tenant_id, book, periods, today):
        self.today = today
        self.live = _live_rows(book)
        lo = min(p.start for p in periods) - timedelta(days=7)
        self.days = sorted(PortfolioSnapshot.objects.filter(
            tenant_id=tenant_id, day__gte=lo, day__lte=today)
            .values_list("day", flat=True).distinct())
        self.first_day = (PortfolioSnapshot.objects.filter(tenant_id=tenant_id)
                          .order_by("day").values_list("day", flat=True).first())
        wanted = set()
        for p in periods:
            for d in (self._opening_day(p), self._closing_day(p)):
                if d and d != "live":
                    wanted.add(d)
        self.rows = _snapshot_rows(tenant_id, wanted)

    def _opening_day(self, p):
        before = [d for d in self.days if p.start - timedelta(days=7) <= d < p.start]
        if before:
            return before[-1]
        within = [d for d in self.days if p.start <= d <= min(p.end, self.today)]
        return within[0] if within else None

    def _closing_day(self, p):
        if p.end >= self.today:
            return "live"
        within = [d for d in self.days if p.start <= d <= p.end]
        return within[-1] if within else None

    def opening(self, p):
        """-> (day | None, rows | None). The day is the snapshot actually used."""
        d = self._opening_day(p)
        return (d, self.rows.get(d, [])) if d else (None, None)

    def closing(self, p):
        d = self._closing_day(p)
        if d == "live":
            return self.today, self.live
        return (d, self.rows.get(d, [])) if d else (None, None)


# ---- the report -------------------------------------------------------------------------

def _money_sum(rows, key="total"):
    return sum((r[key] or Decimal(0) for r in rows), Decimal(0))


def _bucket_sum(rows):
    out = {b: Decimal(0) for b in BUCKETS}
    for r in rows:
        for b in BUCKETS:
            out[b] += r["buckets"].get(b) or Decimal(0)
    return out


def _user_names(ids):
    ids = {i for i in ids if i}
    return {u.id: (u.get_full_name() or u.email) for u in User.objects.filter(id__in=ids)}


def build_report(tenant_id, period, scope, today=None, reason_filter=""):
    today = today or timezone.localdate()
    now = timezone.now()
    portfolio.ensure_snapshot(tenant_id)
    book = portfolio.load_book(tenant_id)
    resolver = book["resolver"]
    alloc = book["alloc"]

    cat_cache = dict(book["category_of"])

    def cat_of(cid, name=""):
        if cid not in cat_cache:
            cat_cache[cid] = resolver.resolve(cid, name)
        return cat_cache[cid]

    def cat_id(cid, name=""):
        return cat_of(cid, name)[0]

    def current_admin(cid):
        a = alloc.get(cid)
        return a.id if a else None

    def row_in_scope(r):
        return (r["status"] in OPEN_STATUSES and scope.clerk_ok(r["admin_id"])
                and scope.category_ok(cat_id(r["cid"], r["name"])))

    periods = trend_periods(period)
    prev = period.previous()
    balances = _Balances(tenant_id, book, periods + [prev], today)

    # ---- events across the whole trend window, filtered per period below ----
    window_start, window_end = periods[0].start_dt(), period.end_dt()
    if prev.start_dt() < window_start:
        window_start = prev.start_dt()

    recoveries = []
    for r in (RecoveredInvoice.objects
              .filter(tenant_id=tenant_id, recovered_at__gte=window_start, recovered_at__lt=window_end)
              .values("contact_id", "contact_name", "amount", "credited", "administrator_id",
                      "allocated_admin_id", "reason", "recovered_at")):
        cid = r["contact_id"] or r["contact_name"] or "Unknown"
        recoveries.append(dict(r, cid=cid, cat=cat_id(cid, r["contact_name"]),
                               stage=_REASON_STAGE.get(r["reason"], ACTIVE)))

    writeoffs = []
    for w in (WriteOffInvoice.objects
              .filter(tenant_id=tenant_id, written_off_at__gte=window_start, written_off_at__lt=window_end)
              .values("contact_id", "contact_name", "amount", "written_off_at")):
        cid = w["contact_id"] or w["contact_name"] or "Unknown"
        writeoffs.append(dict(w, cid=cid, cat=cat_id(cid, w["contact_name"]), admin=current_admin(cid)))

    actions = []
    for c in (CallLog.objects
              .filter(tenant_id=tenant_id, called_at__gte=window_start, called_at__lt=window_end)
              .values("contact_id", "contact_name", "action_type", "called_by_id", "called_at")):
        cid = c["contact_id"] or c["contact_name"] or "Unknown"
        actions.append({"cid": cid, "kind": c["action_type"] or CallLog.ACTION_CALL,
                        "actor": c["called_by_id"], "at": c["called_at"],
                        "cat": cat_id(cid, c["contact_name"]), "admin": current_admin(cid)})
    for c in (DebtorComment.objects
              .filter(tenant_id=tenant_id, deleted_at__isnull=True,
                      created_at__gte=window_start, created_at__lt=window_end)
              .values("contact_id", "contact_name", "author_id", "created_at")):
        cid = c["contact_id"] or c["contact_name"] or "Unknown"
        actions.append({"cid": cid, "kind": "comment", "actor": c["author_id"], "at": c["created_at"],
                        "cat": cat_id(cid, c["contact_name"]), "admin": current_admin(cid)})

    bounds = {}

    def in_period(dt, p):
        # Every event is tested against every period for every clerk and
        # category row, so the period's datetimes are worked out once.
        if p not in bounds:
            bounds[p] = (p.start_dt(), p.end_dt())
        lo, hi = bounds[p]
        return lo <= dt < hi

    def rec_ok(r, clerk=scope.clerk, category=scope.category):
        s = Scope(clerk, category)
        return s.clerk_ok(r["allocated_admin_id"]) and s.category_ok(r["cat"])

    def wo_ok(w, clerk=scope.clerk, category=scope.category):
        s = Scope(clerk, category)
        return s.clerk_ok(w["admin"]) and s.category_ok(w["cat"])

    def action_ok(a, clerk=scope.clerk, category=scope.category):
        s = Scope(clerk, category)
        if clerk is None:
            who_ok = True
        elif clerk == "unallocated":
            who_ok = a["admin"] is None
        else:
            who_ok = a["actor"] == clerk
        return who_ok and s.category_ok(a["cat"])

    def flows(p, clerk=scope.clerk, category=scope.category):
        recs = [r for r in recoveries if in_period(r["recovered_at"], p) and rec_ok(r, clerk, category)]
        wos = [w for w in writeoffs if in_period(w["written_off_at"], p) and wo_ok(w, clerk, category)]
        acts = [a for a in actions if in_period(a["at"], p) and action_ok(a, clerk, category)]
        collected = _money_sum(recs, "amount")
        return {
            "collected": collected,
            "collected_active": _money_sum([r for r in recs if r["stage"] == ACTIVE], "amount"),
            "collected_handover": _money_sum([r for r in recs if r["stage"] == HANDOVER], "amount"),
            "collected_legal": _money_sum([r for r in recs if r["stage"] == LEGAL], "amount"),
            "credited": _money_sum([r for r in recs if r["credited"]
                                    and r["reason"] == RecoveredInvoice.REASON_COLLECTED], "amount"),
            "payments": len(recs),
            "written_off": _money_sum(wos, "amount"),
            "write_off_count": len(wos),
            "calls": sum(1 for a in acts if a["kind"] == CallLog.ACTION_CALL),
            "whatsapps": sum(1 for a in acts if a["kind"] == CallLog.ACTION_WHATSAPP),
            "emails": sum(1 for a in acts if a["kind"] == CallLog.ACTION_EMAIL),
            "comments": sum(1 for a in acts if a["kind"] == "comment"),
            "actions": len(acts),
            "touched": {a["cid"] for a in acts},
        }

    def recovery_rate(f, closing_rows):
        """Collected / (collected + written off + still owed at period end)."""
        if closing_rows is None:
            return None
        owed = _money_sum(closing_rows)
        return pct(f["collected"], f["collected"] + f["written_off"] + owed)

    # Accounts followed up = anyone logged an action on them in the period,
    # whoever did it - the question is whether the account was worked.
    period_touched = {a["cid"] for a in actions if in_period(a["at"], period)}
    prev_touched = {a["cid"] for a in actions if in_period(a["at"], prev)}

    # ---- live portfolio in scope ----
    live_rows = [r for r in balances.live if row_in_scope(r)]
    positions = [p for p in book["positions"]
                 if p["status"] in OPEN_STATUSES and scope.clerk_ok(p["admin_id"])
                 and scope.category_ok(p["category_id"])]

    def portfolio_split(pos):
        out = {}
        for st in OPEN_STATUSES:
            ps = [p for p in pos if p["status"] == st]
            out[st] = {"accounts": len({p["cid"] for p in ps}), "total": _money_sum(ps),
                       "overdue": _money_sum(ps, "overdue"), "invoices": sum(p["invoice_count"] for p in ps)}
        out["all"] = {"accounts": len({p["cid"] for p in pos}), "total": _money_sum(pos),
                      "overdue": _money_sum(pos, "overdue"), "invoices": sum(p["invoice_count"] for p in pos)}
        return out

    def followed(pos, touched):
        """Active accounts with money overdue, and how many of them were worked."""
        due = [p for p in pos if p["status"] == ACTIVE and p["overdue"] > 0]
        worked = [p for p in due if p["cid"] in touched]
        return {"due_accounts": len(due), "due_value": _money_sum(due, "overdue"),
                "accounts": len(worked), "value": _money_sum(worked, "overdue"),
                "pct": pct(len(worked), len(due))}

    # ---- escalations (current state) ----
    escalations = _escalations(tenant_id, book, now)
    esc_in_scope = [e for e in escalations
                    if scope.clerk_ok(e["admin_id"]) and scope.category_ok(e["category_id"])]
    reason_counts = defaultdict(lambda: {"count": 0, "value": Decimal(0)})
    for e in esc_in_scope:
        for key, _label in e["reasons"]:
            reason_counts[key]["count"] += 1
            reason_counts[key]["value"] += e["total"]
    reason_summary = [{"key": k, "label": lbl, **reason_counts[k]}
                      for k, lbl in ESCALATION_REASONS if reason_counts[k]["count"]]
    esc_listed = [e for e in esc_in_scope
                  if not reason_filter or any(k == reason_filter for k, _ in e["reasons"])]

    # ---- summary ----
    f_now, f_prev = flows(period), flows(prev)
    close_day, close_rows = balances.closing(period)
    open_day, open_rows = balances.opening(period)
    pclose_day, pclose_rows = balances.closing(prev)
    close_rows = [r for r in close_rows if row_in_scope(r)] if close_rows is not None else None
    open_rows = [r for r in open_rows if row_in_scope(r)] if open_rows is not None else None
    pclose_rows = [r for r in pclose_rows if row_in_scope(r)] if pclose_rows is not None else None

    split = portfolio_split(positions)
    summary = {
        "portfolio": split,
        "flows": f_now,
        "prev_flows": f_prev,
        "recovery": recovery_rate(f_now, close_rows),
        "prev_recovery": recovery_rate(f_prev, pclose_rows),
        "followed": followed(positions, period_touched),
        "prev_followed": followed(positions, prev_touched),
        "escalations": len(esc_in_scope),
        "escalation_value": _money_sum(esc_in_scope),
        "collected_delta": f_now["collected"] - f_prev["collected"],
        "collected_delta_abs": abs(f_now["collected"] - f_prev["collected"]),
        "actions_delta": f_now["actions"] - f_prev["actions"],
        "actions_delta_abs": abs(f_now["actions"] - f_prev["actions"]),
    }
    rec_now, rec_prev = summary["recovery"], summary["prev_recovery"]
    summary["recovery_delta"] = (rec_now - rec_prev) if (rec_now is not None and rec_prev is not None) else None
    summary["recovery_delta_abs"] = abs(summary["recovery_delta"]) if summary["recovery_delta"] is not None else None

    # ---- per clerk ----
    # Anyone who holds, or held, a portfolio. Somebody who only commented (a
    # lawyer, say) still counts in the totals but does not get a clerk row.
    admin_ids = ({p["admin_id"] for p in book["positions"]}
                 | {r["allocated_admin_id"] for r in recoveries}
                 | set(User.objects.filter(role__in=["administrator", "super_admin"], is_active=True)
                       .values_list("id", flat=True)))
    names = _user_names(admin_ids)
    esc_by_admin = defaultdict(list)
    for e in esc_in_scope:
        esc_by_admin[e["admin_id"]].append(e)

    clerk_keys = [scope.clerk] if scope.clerk is not None else sorted(
        {i for i in admin_ids if i}, key=lambda i: names.get(i, "").lower()) + ["unallocated"]
    clerk_rows = []
    for key in clerk_keys:
        admin_id = None if key == "unallocated" else key
        pos = [p for p in positions if p["admin_id"] == admin_id]
        f = flows(period, clerk=key)
        fp = flows(prev, clerk=key)
        c_rows = [r for r in close_rows if r["admin_id"] == admin_id] if close_rows is not None else None
        sp = portfolio_split(pos)
        if not (sp["all"]["accounts"] or f["collected"] or f["actions"] or f["written_off"]):
            continue
        overdue_90 = sum(((p["buckets"]["91-120"] + p["buckets"]["120+"]) for p in pos
                          if p["status"] == ACTIVE), Decimal(0))
        clerk_rows.append({
            "key": key, "admin_id": admin_id,
            "name": portfolio.UNALLOCATED if admin_id is None else names.get(admin_id, "(removed user)"),
            "portfolio": sp, "flows": f, "prev_collected": fp["collected"],
            "recovery": recovery_rate(f, c_rows),
            "followed": followed(pos, period_touched),
            "active_90_pct": pct(overdue_90, sp[ACTIVE]["total"]),
            "escalations": len(esc_by_admin.get(admin_id, [])),
            "escalation_value": _money_sum(esc_by_admin.get(admin_id, [])),
        })

    # ---- per category ----
    cats = [(c.id, c.name) for c in resolver.all
            if c.is_active or any(p["category_id"] == c.id for p in positions)]
    cat_keys = ([(scope.category, None)] if scope.category is not None
                else [(cid_, nm) for cid_, nm in cats] + [("none", portfolio.UNCATEGORISED)])
    category_rows = []
    for key, name in cat_keys:
        category_id = None if key == "none" else key
        if name is None:
            name = (portfolio.UNCATEGORISED if category_id is None
                    else resolver.by_id[category_id].name if category_id in resolver.by_id else "?")
        pos = [p for p in positions if p["category_id"] == category_id]
        f = flows(period, category=key)
        fp = flows(prev, category=key)
        c_rows = ([r for r in close_rows if cat_id(r["cid"], r["name"]) == category_id]
                  if close_rows is not None else None)
        sp = portfolio_split(pos)
        esc = [e for e in esc_in_scope if e["category_id"] == category_id]
        # Every category stays on the list, so an empty one shows as empty
        # rather than silently missing.
        overdue_90 = sum(((p["buckets"]["91-120"] + p["buckets"]["120+"]) for p in pos
                          if p["status"] != LEGAL), Decimal(0))
        category_rows.append({
            "key": key, "category_id": category_id, "name": name,
            "portfolio": sp, "flows": f, "prev_collected": fp["collected"],
            "recovery": recovery_rate(f, c_rows),
            "followed": followed(pos, period_touched),
            "aged_90_pct": pct(overdue_90, sp[ACTIVE]["total"] + sp[HANDOVER]["total"]),
            "escalations": len(esc), "escalation_value": _money_sum(esc),
        })

    # ---- ageing now, by status ----
    ageing_rows = []
    for st in OPEN_STATUSES:
        rows = [r for r in live_rows if r["status"] == st]
        b = _bucket_sum(rows)
        ageing_rows.append({"status": st, "label": portfolio.STATUS_LABELS[st],
                            "buckets": [b[k] for k in BUCKETS], "total": _money_sum(rows)})
    all_b = _bucket_sum(live_rows)
    ageing_total = {"buckets": [all_b[k] for k in BUCKETS], "total": _money_sum(live_rows)}

    # ---- movement over the period ----
    movement = None
    if close_rows is not None:
        closing = _money_sum(close_rows)
        cb = _bucket_sum(close_rows)
        movement = {"closing": closing, "closing_day": close_day,
                    "collected": f_now["collected"], "written_off": f_now["written_off"],
                    "opening": None, "opening_day": open_day, "new": None,
                    "opening_partial": bool(open_day and open_day >= period.start),
                    "buckets": [], "statuses": []}
        if open_rows is not None:
            opening = _money_sum(open_rows)
            ob = _bucket_sum(open_rows)
            movement["opening"] = opening
            # What is left after collections and write-offs are accounted for:
            # new invoices raised, credit notes and other adjustments.
            movement["new"] = closing - opening + f_now["collected"] + f_now["written_off"]
            movement["new_abs"] = abs(movement["new"])
            movement["buckets"] = [{"label": BUCKET_PLAIN[b], "opening": ob[b], "closing": cb[b],
                                    "change": cb[b] - ob[b], "change_abs": abs(cb[b] - ob[b])}
                                   for b in BUCKETS]
            for st in OPEN_STATUSES:
                o = _money_sum([r for r in open_rows if r["status"] == st])
                c = _money_sum([r for r in close_rows if r["status"] == st])
                movement["statuses"].append({"label": portfolio.STATUS_LABELS[st],
                                             "opening": o, "closing": c, "change": c - o,
                                             "change_abs": abs(c - o)})

    # Clients who went to (or came back from) the attorneys in the period.
    owed_now = defaultdict(Decimal)
    for p in book["positions"]:
        if p["status"] in OPEN_STATUSES:
            owed_now[p["cid"]] += p["total"]
    legal_moves = {"approved": [], "closed": []}
    for m in LegalMatter.objects.filter(tenant_id=tenant_id).only(
            "contact_id", "contact_name", "approved_at", "closed_at", "status"):
        cid = m.contact_id or m.contact_name
        if not (scope.clerk_ok(current_admin(cid)) and scope.category_ok(cat_id(cid, m.contact_name))):
            continue
        entry = {"name": m.contact_name or cid, "owed": owed_now.get(cid, Decimal(0)), "id": m.id}
        if m.approved_at and in_period(m.approved_at, period):
            legal_moves["approved"].append(entry)
        if m.closed_at and in_period(m.closed_at, period):
            legal_moves["closed"].append(entry)
    handover_new = set()
    for cid, name in (HandoverInvoice.objects
                      .filter(tenant_id=tenant_id, marked_at__gte=period.start_dt(),
                              marked_at__lt=period.end_dt())
                      .values_list("contact_id", "contact_name")):
        key = cid or name
        if scope.clerk_ok(current_admin(key)) and scope.category_ok(cat_id(key, name)):
            handover_new.add(key)

    # ---- trend ----
    trend = []
    for p in periods:
        f = flows(p)
        d, rows = balances.closing(p)
        rows = [r for r in rows if row_in_scope(r)] if rows is not None else None
        trend.append({"period": p, "label": p.label, "short": p.short, "flows": f,
                      "closing": _money_sum(rows) if rows is not None else None,
                      "closing_buckets": _bucket_sum(rows) if rows is not None else None,
                      "recovery": recovery_rate(f, rows),
                      "followed_accounts": len(f["touched"]),
                      "current": p == period})

    # ---- with the attorneys ----
    legal_rows = _legal_rows(tenant_id, book, now, scope, recoveries, period, in_period)

    return {
        "period": period, "prev": prev, "next": period.next(), "today": today,
        "is_future": period.start > today,
        "scope": scope,
        "summary": summary,
        "clerk_rows": clerk_rows,
        "category_rows": category_rows,
        "ageing_rows": ageing_rows, "ageing_total": ageing_total, "buckets": BUCKETS,
        "bucket_plain": [BUCKET_PLAIN[b] for b in BUCKETS],
        "movement": movement,
        "legal_moves": legal_moves,
        "handover_new": len(handover_new),
        "trend": trend,
        "collected_chart": column_chart([(t["label"], t["short"], t["flows"]["collected"]) for t in trend]),
        "outstanding_chart": stacked_chart([(t["label"], t["short"], t["closing_buckets"]) for t in trend]),
        "escalations": esc_listed,
        "escalation_reasons": reason_summary,
        "reason_filter": reason_filter if reason_filter in ESCALATION_LABELS else "",
        "legal_rows": legal_rows,
        "history_from": balances.first_day,
    }


def _last_actions(tenant_id):
    """cid -> datetime of the last call / WhatsApp / email / comment, all time."""
    out = {}
    for r in (CallLog.objects.filter(tenant_id=tenant_id)
              .values("contact_id", "contact_name").annotate(last=Max("called_at"))):
        cid = r["contact_id"] or r["contact_name"] or "Unknown"
        if r["last"] and (cid not in out or r["last"] > out[cid]):
            out[cid] = r["last"]
    for r in (DebtorComment.objects.filter(tenant_id=tenant_id, deleted_at__isnull=True)
              .values("contact_id").annotate(last=Max("created_at"))):
        cid = r["contact_id"]
        if r["last"] and (cid not in out or r["last"] > out[cid]):
            out[cid] = r["last"]
    return out


def _matters_by_key(tenant_id):
    """debtor key -> LegalMatter (non-closed), under both contact_id and name."""
    out = {}
    for m in (LegalMatter.objects.filter(tenant_id=tenant_id)
              .exclude(status=LegalMatter.CLOSED)
              .prefetch_related("step_states", "step_comments")):
        for key in (m.contact_id, m.contact_name):
            if key:
                out.setdefault(key, m)
    return out


def _escalations(tenant_id, book, now):
    """Every open account that needs someone to act, with the reasons why."""
    from .reports import _last_activity

    last_action = _last_actions(tenant_id)
    matters = _matters_by_key(tenant_id)
    active_invoices = [inv for inv in book["invoices"] if inv["status"] == ACTIVE]
    missed = portfolio.missed_invoice_ids(tenant_id, active_invoices)
    stale_before = now - timedelta(days=NO_CONTACT_DAYS)

    out = []
    for p in book["positions"]:
        reasons = []
        matter = matters.get(p["cid"])
        extra = {}
        if p["status"] == ACTIVE:
            if any(inv["invoice_id"] in missed for inv in p["invoices"]):
                reasons.append("missed")
            if p["max_dpd"] >= ESCALATE_DPD:
                reasons.append("aged")
            last = last_action.get(p["cid"])
            if p["overdue"] > 0 and (last is None or last < stale_before):
                reasons.append("no_contact")
            if p["overdue"] > 0 and p["admin_id"] is None:
                reasons.append("unallocated")
        elif p["status"] == HANDOVER:
            if matter and matter.status == LegalMatter.PENDING:
                reasons.append("legal_pending")
            else:
                reasons.append("handover_decision")
        elif p["status"] == LEGAL and matter:
            last = _last_activity(matter)
            idle = (now - last).days if last else 0
            extra["days_idle"] = idle
            if idle >= LEGAL_IDLE_DAYS:
                reasons.append("legal_idle")
        if not reasons:
            continue
        out.append({
            "cid": p["cid"], "name": p["name"], "status": p["status"],
            "status_label": p["status_label"], "admin_id": p["admin_id"],
            "admin_name": p["admin_name"] or portfolio.UNALLOCATED,
            "category_id": p["category_id"], "category_name": p["category_name"],
            "total": p["total"], "overdue": p["overdue"], "max_dpd": p["max_dpd"],
            "last_action": last_action.get(p["cid"]),
            "matter_id": matter.id if matter else None,
            "reasons": [(k, ESCALATION_LABELS[k]) for k in reasons],
            "todo": [ESCALATION_ACTIONS[k] for k in reasons],
            # Each reason with the step that answers it, for the attention list.
            "steps": [(ESCALATION_LABELS[k], ESCALATION_ACTIONS[k]) for k in reasons], **extra,
        })
    out.sort(key=lambda e: e["total"], reverse=True)
    return out


def _legal_rows(tenant_id, book, now, scope, recoveries, period, in_period):
    """Accounts with the attorneys, for monitoring alongside the clerks' books."""
    from .reports import _last_activity

    matters = _matters_by_key(tenant_id)
    recovered = defaultdict(Decimal)
    for r in recoveries:
        if r["stage"] == LEGAL and in_period(r["recovered_at"], period):
            recovered[r["cid"]] += r["amount"]
    rows = []
    for p in book["positions"]:
        if p["status"] != LEGAL:
            continue
        if not (scope.clerk_ok(p["admin_id"]) and scope.category_ok(p["category_id"])):
            continue
        m = matters.get(p["cid"])
        last = _last_activity(m) if m else None
        rows.append({
            "cid": p["cid"], "name": p["name"], "admin_name": p["admin_name"] or portfolio.UNALLOCATED,
            "category_name": p["category_name"], "total": p["total"], "invoices": p["invoice_count"],
            "max_dpd": p["max_dpd"], "matter_id": m.id if m else None,
            "approved_at": m.approved_at if m else None,
            "days_with_attorneys": (now - m.approved_at).days if m and m.approved_at else None,
            "last_activity": last, "days_idle": (now - last).days if last else None,
            "recovered": recovered.get(p["cid"], Decimal(0)),
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows
