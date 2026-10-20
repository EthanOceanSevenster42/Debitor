"""Where each legal matter stands and what has to happen next.

Every matter sits in exactly one stage tab on the Lawyers page - New handovers,
Letters of demand issued, LODs hand delivered, No trace, In litigation, Promises
to pay, Payment arrangements or Closed - so the tabs add up to the whole book.

Each active matter also gets its TASKS: the next things to do, each with who does
it and by when. They drive the rest of the page:

    * Action required - the tasks that are overdue or need doing now;
    * Next action     - the earliest task, with its deadline;
    * the Diary       - every dated task, by day;
    * the reminders   - notifications.send_legal_diary_reminders.

Promises and arrangements are checked against the payments received: those the
Xero sync picks up (RecoveredInvoice) and those the attorneys record themselves
(LegalPayment), taking whichever total is larger so nothing is counted twice.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING

from django.utils import timezone

from . import legal_workflow
from .models import LegalMatter, LegalPaymentPlan, RecoveredInvoice

# Days after a promised or instalment date before a missing payment counts as
# missed, so a payment that reaches Xero a day or two late raises no alarm.
PAYMENT_GRACE_DAYS = 3
# The attorneys should send the Letter of Demand within this many days.
NEW_MATTER_ACTION_DAYS = 7
# The debtor's time to respond to the Letter of Demand.
LOD_RESPONSE_DAYS = 28
# No activity for this long (after the LOD grace period) needs a look.
IDLE_ACTION_DAYS = 14
# After default listing, the summons or application should follow within this.
LITIGATION_AFTER_LISTING_DAYS = 14
# An unsuccessful delivery should be traced, or closure recommended, within this.
NO_TRACE_ACTION_DAYS = 14
# Reminders of an instalment go out this many days before it is due.
INSTALMENT_REMINDER_DAYS = 2

ACTION = "action"
PENDING = "pending"
NEW = "new"
LOD_ISSUED = "lod_issued"
HAND_DELIVERED = "hand_delivered"
NO_TRACE = "no_trace"
LITIGATION = "litigation"
PROMISE = "promise"
ARRANGEMENT = "arrangement"
CLOSED = "closed"
ALL = "all"

# (key, tab label, what it means) in the order the tabs show.
STAGES = [
    (ACTION, "Action required", "Overdue or needing attention now - each says what must be done and by whom."),
    (NEW, "New handovers", "Newly handed over. The attorneys still need to send the Letter of Demand."),
    (LOD_ISSUED, "Letters of demand issued", "The Letter of Demand went out by email or sheriff. Waiting for hand delivery or the debtor's response."),
    (HAND_DELIVERED, "LODs hand delivered", "An FSA inspector delivered the Letter of Demand by hand and the signed proof is on file."),
    (NO_TRACE, "No trace / unsuccessful delivery", "The facility could not be found, was closed, or the delivery failed."),
    (LITIGATION, "In litigation", "Summons issued or an application for payment launched."),
    (PROMISE, "Promises to pay", "The debtor committed to paying by a specific date."),
    (ARRANGEMENT, "Payment arrangements", "The debtor is paying off the debt in agreed instalments."),
    (CLOSED, "Closed matters", "Finalised, settled, or found unrecoverable."),
    (PENDING, "Awaiting approval", "Sent to the lawyers by FSA; a Super Admin must approve before the attorneys see it."),
    (ALL, "All matters", "Every matter on the Lawyers page."),
]
STAGE_LABELS = {k: label for k, label, _ in STAGES}
STAGE_HINTS = {k: hint for k, _, hint in STAGES}

OWNER_FSA = "FSA"
OWNER_ATTORNEYS = "Attorneys"

# Anything past the Collections letters: summons issued or an application launched.
LITIGATION_KEYS = (legal_workflow.ALL_STEP_KEYS
                   - {t[0] for t in legal_workflow.COLLECTIONS}) | {"issue_summons_or_application"}


# ---- IMI / APS ----------------------------------------------------------------------

# Xero project codes: meat inspection at the abattoirs is IMI; product standards
# at retail (raw and processed meat products, eggs) is APS.
IMI_PROJECT_CODES = ("010", "020")
APS_PROJECT_CODES = ("050", "060", "070")
IMI_CATEGORY_WORDS = ("abattoir",)
APS_CATEGORY_WORDS = ("store", "butcher", "correctional", "retail")
DIVISION_LABELS = {LegalMatter.IMI: "IMI", LegalMatter.APS: "APS", "": "Not set"}


def guess_division(project_codes, category_name):
    """IMI, APS or "" from the debtor's Xero project codes (each like
    "010 - Red Meat", possibly several joined by commas) and its category."""
    codes = set()
    for raw in project_codes:
        for part in (raw or "").split(","):
            codes.add(part.strip()[:3])
    if codes & set(IMI_PROJECT_CODES):
        return LegalMatter.IMI
    if codes & set(APS_PROJECT_CODES):
        return LegalMatter.APS
    cat = (category_name or "").lower()
    if any(w in cat for w in IMI_CATEGORY_WORDS):
        return LegalMatter.IMI
    if any(w in cat for w in APS_CATEGORY_WORDS):
        return LegalMatter.APS
    return ""


# ---- payments, promises and arrangements --------------------------------------------

def _local_date(dt):
    return timezone.localtime(dt).date() if dt else None


def _add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def instalment_date(plan, k):
    """The date of instalment number k (0-based) of a plan."""
    if plan.kind == LegalPaymentPlan.PROMISE:
        return plan.first_date
    if plan.frequency == LegalPaymentPlan.WEEKLY:
        return plan.first_date + timedelta(days=7 * k)
    if plan.frequency == LegalPaymentPlan.FORTNIGHTLY:
        return plan.first_date + timedelta(days=14 * k)
    return _add_months(plan.first_date, k)


def instalment_count(plan):
    """How many instalments the plan has, or None when it is open-ended."""
    if plan.kind == LegalPaymentPlan.PROMISE:
        return 1
    if plan.instalments:
        return plan.instalments
    if plan.total_amount and plan.amount:
        return int((plan.total_amount / plan.amount).to_integral_value(ROUND_CEILING))
    return None


def plan_total(plan):
    if plan.kind == LegalPaymentPlan.PROMISE:
        return plan.amount
    if plan.total_amount:
        return plan.total_amount
    n = instalment_count(plan)
    return plan.amount * n if n else None


def payments_by_contact(tenant_id, matters):
    """Payments Xero shows from each matter's debtor: {matter.id: [(date, amount)]}.
    Matched on contact id, falling back to the name, as the rest of the legal
    pages do."""
    ids = {m.contact_id for m in matters if m.contact_id}
    by_id, by_name = {}, {}
    for cid, at, amount in (RecoveredInvoice.objects.filter(tenant_id=tenant_id, contact_id__in=ids)
                            .values_list("contact_id", "recovered_at", "amount")):
        by_id.setdefault(cid, []).append((_local_date(at), amount))
    names = {m.contact_name for m in matters if m.contact_name and m.contact_id not in by_id}
    if names:
        for cname, at, amount in (RecoveredInvoice.objects
                                  .filter(tenant_id=tenant_id, contact_name__in=names)
                                  .values_list("contact_name", "recovered_at", "amount")):
            by_name.setdefault(cname, []).append((_local_date(at), amount))
    return {m.id: by_id.get(m.contact_id) or by_name.get(m.contact_name) or [] for m in matters}


def received_since(since, xero_payments, recorded_payments):
    """Money in since a date: the larger of what Xero shows and what the
    attorneys recorded, so a payment reported by both counts once."""
    xero = sum((amt for d, amt in xero_payments if d and d >= since), Decimal(0))
    recorded = sum((p.amount for p in recorded_payments if p.paid_on >= since), Decimal(0))
    return max(xero, recorded)


def plan_progress(plan, xero_payments, today, recorded_payments=()):
    """How a promise or arrangement is going, from the payments received since it
    was recorded:

        received, total, balance (still to pay on the plan), expected (what should
        be in by now), shortfall, behind (bool), paid_in_full (bool),
        next_date / next_amount (the next payment to come), paid_count /
        due_count / count (instalments; count None if open-ended), and
        schedule: [{"n", "date", "amount", "status": paid|missed|due|upcoming}].
    """
    since = _local_date(plan.created_at) or today
    received = received_since(since, xero_payments, recorded_payments)
    grace_cutoff = today - timedelta(days=PAYMENT_GRACE_DAYS)
    total = plan_total(plan)
    n = instalment_count(plan)
    amount = plan.amount or Decimal(0)

    # The instalments: all of them, or for an open-ended plan enough to show
    # what is due and the next few.
    rows, cum = [], Decimal(0)
    k = 0
    while True:
        if n is not None and k >= n:
            break
        d = instalment_date(plan, k)
        if n is None and d > today and rows and rows[-1]["status"] == "upcoming":
            break       # open-ended: up to the next instalment still to come
        amt = amount
        if total and n and k == n - 1:
            amt = total - amount * (n - 1)          # the last one takes the remainder
        cum += amt
        if cum <= received:
            status = "paid"
        elif d < grace_cutoff:
            status = "missed"
        elif d <= today:
            status = "due"
        else:
            status = "upcoming"
        rows.append({"n": k + 1, "date": d, "amount": amt, "status": status, "cumulative": cum})
        k += 1
        if k > 520:     # ten years of weekly payments; a guard, never reached
            break

    expected = sum((r["amount"] for r in rows if r["date"] < grace_cutoff), Decimal(0))
    paid_in_full = bool(total) and received >= total
    nxt = next((r for r in rows if r["status"] != "paid"), None)
    first_missed = next((r for r in rows if r["status"] == "missed"), None)
    return {
        "received": received, "total": total,
        "balance": max(Decimal(0), total - received) if total else None,
        "expected": expected, "shortfall": max(Decimal(0), expected - received),
        "behind": not paid_in_full and expected > received,
        "paid_in_full": paid_in_full,
        "next_date": None if paid_in_full or not nxt else nxt["date"],
        # What is left of the next instalment once part-payments are counted.
        "next_amount": (Decimal(0) if paid_in_full or not nxt
                        else min(nxt["amount"], nxt["cumulative"] - received)),
        "next_n": nxt["n"] if nxt and not paid_in_full else None,
        "first_missed": first_missed,
        "paid_count": sum(1 for r in rows if r["status"] == "paid"),
        "due_count": sum(1 for r in rows if r["date"] < grace_cutoff),
        "count": n, "schedule": rows,
    }


# ---- where a matter stands, and what to do next ------------------------------------

def _money(v):
    return f"R {v:,.2f}"


def _day(d):
    return f"{d.day} {d:%b %Y}" if d else ""


def attorney_label(matter):
    a = matter.attorney
    return (a.get_full_name() or a.email) if a else OWNER_ATTORNEYS


def _next_litigation_step(matter, done):
    """The first step not yet ticked in the litigation route the matter is on."""
    for sec in legal_workflow.sections_for(matter.summons_opposed, matter.application_opposed)[1:]:
        keys = [s["key"] for s in sec["steps"] if not s["is_variation"]]
        if any(k in done for k in keys):
            for s in sec["steps"]:
                if not s["is_variation"] and s["key"] not in done:
                    return f'{sec["title"]}: {s["label"]}'
    return ""


def board_state(matter, now, payments=(), days_idle=0, owed=None):
    """Where one matter stands and what has to happen next. Reads
    matter.step_states, matter.payment_plans and matter.payments with .all() so
    prefetched rows are reused. ``payments`` are the Xero payments from
    payments_by_contact; ``owed`` what Xero says the debtor still owes.

    Returns {"stage", "stage_label", "detail" (one line on where it stands),
    "tasks": [{"owner", "text", "due", "flag", "overdue", "kind"}],
    "actions" (the tasks flagged as Action required), "next" (the earliest task),
    "plan", "progress", "received" (all money in since the handover)}."""
    today = timezone.localtime(now).date()
    steps = {s.step_key: s for s in matter.step_states.all() if s.done}
    plans = list(matter.payment_plans.all())
    plan = next((p for p in plans if p.status == LegalPaymentPlan.OPEN), None)
    recorded = list(matter.payments.all())
    progress = plan_progress(plan, payments, today, recorded) if plan else None
    served = legal_workflow.lod_served_at(matter)
    delivery = legal_workflow.lod_delivery_state(matter, now)
    hand = steps.get(legal_workflow.LOD_HAND_DELIVERY)
    litigation = sorted((s for k, s in steps.items() if k in LITIGATION_KEYS),
                        key=lambda s: s.done_at or now)
    handed_over = _local_date(matter.approved_at or matter.sent_at) or today

    # ---- the one stage tab it belongs to ----
    if matter.status == LegalMatter.PENDING:
        stage = PENDING
        detail = f"Sent by {matter.sent_by or 'FSA'} on {_day(_local_date(matter.sent_at))}"
    elif matter.status == LegalMatter.CLOSED:
        stage = CLOSED
        reason = matter.get_closure_reason_display() if matter.closure_reason else ""
        detail = (f"Closed on {_day(_local_date(matter.closed_at))}" if matter.closed_at else "Closed")
        if reason:
            detail = f"{reason} - {detail[0].lower()}{detail[1:]}"
    elif plan and plan.kind == LegalPaymentPlan.ARRANGEMENT:
        stage = ARRANGEMENT
        detail = f"{_money(plan.amount)} {plan.get_frequency_display().lower()}"
        if progress["count"]:
            detail += f" - {progress['paid_count']} of {progress['count']} paid"
        if progress["balance"] is not None:
            detail += f" - {_money(progress['balance'])} still to pay"
    elif plan:
        stage = PROMISE
        detail = f"{_money(plan.amount)} promised by {_day(plan.first_date)}"
        if progress["received"]:
            detail += f" - {_money(progress['received'])} received"
    elif litigation:
        stage = LITIGATION
        last = litigation[-1]
        detail = f"{legal_workflow.STEP_LABELS.get(last.step_key, last.step_key)} ({_day(_local_date(last.done_at))})"
    elif matter.delivery_failed and not hand:
        stage = NO_TRACE
        detail = matter.get_delivery_failed_reason_display() or "Delivery unsuccessful"
        if matter.delivery_failed_note:
            detail += f" - {matter.delivery_failed_note}"
    elif hand:
        stage = HAND_DELIVERED
        detail = f"Hand delivered on {_day(_local_date(hand.done_at))}"
        if served:
            ends = _local_date(served) + timedelta(days=LOD_RESPONSE_DAYS)
            detail += (f" - response period ends {_day(ends)}" if ends > today
                       else f" - response period ended {_day(ends)}")
    elif served:
        stage = LOD_ISSUED
        detail = f"Letter of Demand sent on {_day(_local_date(served))}"
    else:
        stage = NEW
        detail = f"Handed over on {_day(handed_over)}"
        if steps:
            detail += " - instruction received"

    # ---- what has to happen next ----
    tasks = []
    lawyers = attorney_label(matter)

    def task(owner, text, due, flag=None, kind="step"):
        overdue = bool(due and due < today)
        tasks.append({"owner": owner, "text": text, "due": due, "kind": kind, "overdue": overdue,
                      "flag": overdue if flag is None else flag})

    if matter.status == LegalMatter.ACTIVE:
        if matter.closure_requested_at:
            task(OWNER_FSA, f"Approve or decline the recommended closure "
                            f"({matter.get_closure_requested_reason_display()}) - {matter.closure_requested_by}",
                 _local_date(matter.closure_requested_at), flag=True, kind="closure")
        if owed is not None and owed <= 0 and not matter.closure_requested_at:
            task(lawyers, "Nothing is owed in Xero any more - confirm the settlement and close the matter",
                 today, flag=True, kind="settled")
        if plan:
            if progress["paid_in_full"]:
                task(lawyers, f"{plan.get_kind_display()} paid in full - mark it kept and close the matter",
                     today, flag=True, kind="plan")
            elif progress["behind"]:
                missed = progress["first_missed"]
                if plan.kind == LegalPaymentPlan.PROMISE:
                    text = (f"Promise not kept - {_money(plan.amount)} was due {_day(plan.first_date)}, "
                            f"{_money(progress['received'])} received")
                else:
                    text = (f"Instalment {missed['n']} missed ({_day(missed['date'])}) - "
                            f"arrangement behind by {_money(progress['shortfall'])}")
                task(lawyers, text, missed["date"] if missed else plan.first_date, flag=True, kind="plan")
            elif progress["next_date"]:
                what = ("promised payment" if plan.kind == LegalPaymentPlan.PROMISE
                        else f"instalment {progress['next_n']}")
                task(lawyers, f"Check the {what} of {_money(progress['next_amount'])} has been paid",
                     progress["next_date"], flag=False, kind="plan")
        if matter.follow_up_on:
            note = f": {matter.follow_up_note}" if matter.follow_up_note else ""
            task(matter.follow_up_by or lawyers, f"Follow-up{note}", matter.follow_up_on,
                 flag=matter.follow_up_on <= today, kind="follow_up")

        # While a promise or arrangement is open it decides what happens next: a
        # missed payment is flagged above, so the workflow steps wait.
        if not plan:
            if stage == NEW:
                days = (today - handed_over).days
                task(lawyers, f"Send the Letter of Demand (handed over {days} day{'s' if days != 1 else ''} ago)",
                     handed_over + timedelta(days=NEW_MATTER_ACTION_DAYS))
            if served and not hand and not matter.delivery_failed and not litigation:
                due = _local_date(delivery["due_at"])
                if delivery["outstanding"]:
                    task(OWNER_FSA, f"Hand delivery outstanding since {_day(due)} - an inspector must deliver "
                                    "the LOD and upload the signed proof", due, flag=True)
                else:
                    task(OWNER_FSA, "Have an inspector hand-deliver the LOD and upload the signed proof", due)
            if stage == NO_TRACE:
                task(lawyers, "Trace the facility, or recommend closure",
                     _local_date(matter.delivery_failed_at or now) + timedelta(days=NO_TRACE_ACTION_DAYS))
            listing = steps.get("default_listing_28") or steps.get("debtor_listed")
            if served and not litigation:
                ends = _local_date(served) + timedelta(days=LOD_RESPONSE_DAYS)
                if not listing:
                    if ends <= today:
                        task(lawyers, "28 days since the LOD - proceed with default listing or summons",
                             ends, flag=True)
                    else:
                        task(lawyers, "Default listing or summons once the 28-day response period ends", ends)
                else:
                    task(lawyers, "Issue summons or launch the application for payment",
                         _local_date(listing.done_at or now) + timedelta(days=LITIGATION_AFTER_LISTING_DAYS))
            if litigation:
                nxt = _next_litigation_step(matter, steps)
                if nxt:
                    last = max((s.done_at for s in litigation if s.done_at), default=now)
                    task(lawyers, f"Next: {nxt}", _local_date(last) + timedelta(days=IDLE_ACTION_DAYS), flag=False)
            if days_idle >= IDLE_ACTION_DAYS:
                task(lawyers, f"No activity for {days_idle} days", today, flag=True, kind="idle")

    far = date.max
    tasks.sort(key=lambda t: (t["due"] or far, not t["flag"]))
    received = received_since(handed_over, payments, recorded)
    return {"stage": stage, "stage_label": STAGE_LABELS[stage], "detail": detail,
            "tasks": tasks, "actions": [t for t in tasks if t["flag"]],
            "next": tasks[0] if tasks else None,
            "plan": plan, "progress": progress, "received": received}


def in_tab(state, tab):
    if tab == ALL:
        return True
    if tab == ACTION:
        return bool(state["actions"])
    return state["stage"] == tab


# The workflow path shown across the top of a matter, Salesforce-style: where the
# matter is and what comes next. Side stages (promise, arrangement, no trace)
# show as a note beside it instead of a step on the path.
PATH = [(NEW, "New handover"), (LOD_ISSUED, "LOD issued"), (HAND_DELIVERED, "LOD hand delivered"),
        (LITIGATION, "In litigation"), (CLOSED, "Closed")]


def path_for(matter, state):
    """[{"label", "state": "done"|"current"|"skipped"|"todo"}] for the matter's
    path. "skipped" is a stage the matter moved past without it being recorded,
    such as going to court without the LOD being hand delivered."""
    steps = {s.step_key for s in matter.step_states.all() if s.done}
    reached = {NEW}
    if steps & set(legal_workflow.LOD_SERVED_KEYS):
        reached.add(LOD_ISSUED)
    if legal_workflow.LOD_HAND_DELIVERY in steps:
        reached.add(HAND_DELIVERED)
    if steps & LITIGATION_KEYS:
        reached.add(LITIGATION)
    if matter.status == LegalMatter.CLOSED:
        reached.add(CLOSED)
    keys = [k for k, _ in PATH]
    current = max((keys.index(k) for k in reached), default=0)

    def state_of(i, k):
        if i == current:
            return "current"
        if i > current:
            return "todo"
        return "done" if k in reached else "skipped"

    return [{"label": label, "state": state_of(i, k)} for i, (k, label) in enumerate(PATH)]
