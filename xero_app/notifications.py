"""Transactional lawyer notifications (distinct from the weekly digest in
reports.py). Currently: alert the lawyers when a new matter is approved and
becomes active, so they know a new client needs attention.

Sends through the same Graph email path as everything else. Any send failure is
logged and swallowed — a notification must never block the approval itself.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from .mailer import send_app_email
from .models import ReportRecipient

logger = logging.getLogger(__name__)
User = get_user_model()


def _lawyer_recipients():
    """Who to alert: the configured Lawyer Report recipients — the same curated
    "lawyers and management" list as the weekly report, so there's one clean
    place to manage who hears from the lawyers' side. If none are configured yet,
    fall back to active users with the Lawyer role."""
    emails = list(ReportRecipient.objects.filter(is_active=True)
                  .values_list("email", flat=True))
    if emails:
        return emails
    return list(User.objects.filter(role=Role.LAWYER, is_active=True)
                .exclude(email="").values_list("email", flat=True))


def notify_new_matter_approved(matter, approved_by=""):
    """Email the lawyers that a newly handed-over client has been approved and
    needs attention. Returns the number of messages sent (0 if nobody to notify
    or the send failed).

    Goes to every attorney with a login as well as the report recipients, and
    each attorney also gets it in the app, so a new instruction is never missed."""
    from .models import DebtorNotice
    attorneys = _attorneys()
    _raise_notices(matter, attorneys, DebtorNotice.KIND_NEW_MATTER, approved_by or matter.approved_by or "FSA",
                   f"New legal instruction: {matter.contact_name or matter.contact_id} has been handed "
                   f"over to the attorneys and needs attention.")
    name = matter.contact_name or matter.contact_id
    who = approved_by or matter.approved_by or "FSA"
    recipients = attorneys + _lawyer_recipients()
    if not recipients:
        logger.info("New-matter approval for %s: no lawyer/recipient emails to notify.", name)
        return 0
    return _email(f"New legal instruction – {name}", {
        "paragraphs": [
            format_html("Please note that <strong>{}</strong> has been handed over to the attorneys for "
                        "collection. The instruction was approved by {} on {} and is now ready for "
                        "your attention.", name, who, _when(matter.approved_at or timezone.now())),
            "The outstanding invoices and the full history of the account are available on the matter.",
        ],
        "url": _matter_url(matter), "link_text": "view the matter on the Debtor System",
    }, recipients)


# ---- Letter of Demand hand delivery ---------------------------------------------

def _fsa_staff():
    """The FSA people who arrange an inspector: the active Super Admins."""
    return list(User.objects.filter(role=Role.SUPER_ADMIN, is_active=True))


def _legal_team():
    """The attorneys. Somebody given working access to the Lawyers page is not
    on the legal team: they can work matters but do not get the legal updates."""
    return list(User.objects.filter(role=Role.LAWYER, is_active=True))


def _raise_notices(matter, users, kind, actor_name, text):
    """One in-app notice per user (each counted on their Notifications badge),
    linked to the matter so it opens straight onto the workflow."""
    from .models import DebtorNotice
    return [DebtorNotice.objects.create(
        tenant_id=matter.tenant_id, recipient=u, actor_name=actor_name,
        actor_role="", contact_id=matter.contact_id, contact_name=matter.contact_name,
        kind=kind, matter=matter, text=text) for u in users]


def _email(subject, ctx, recipients):
    """Send a legal notice as an ordinary email - one per person, addressed to
    them by first name - with a plain-text copy for mail apps that do not show
    formatting.

    ``recipients`` may mix users and bare email addresses (the lawyer-report
    list); a bare address is greeted "Good day". ``ctx`` fills
    templates/xero/legal_notice_email.*: paragraphs (escaped HTML), quote,
    documents, url and the link wording. Returns how many were sent."""
    people = {}
    for r in recipients:
        email = getattr(r, "email", r)
        if email and (email not in people or not people[email]):
            people[email] = getattr(r, "first_name", "") or ""
    sent = 0
    for email, first_name in sorted(people.items()):
        personal = dict(ctx, greeting_name=first_name)
        try:
            sent += send_app_email(subject=subject,
                                   body=render_to_string("xero/legal_notice_email.txt", personal),
                                   html_body=render_to_string("xero/legal_notice_email.html", personal),
                                   to=[email])
        except Exception:
            logger.exception("Failed to send '%s' to %s", subject, email)
    return sent


def _matter_url(matter):
    return settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id])


def _when(dt):
    """'1 October 2026'."""
    if not dt:
        return ""
    local = timezone.localtime(dt)
    return f"{local.day} {local:%B %Y}"


def notify_lod_delivery_due(matter, now=None):
    """Tell FSA the Letter of Demand is due for hand delivery by an inspector.
    Sent once per matter; returns the notices raised."""
    from .models import DebtorNotice
    now = now or timezone.now()
    from . import legal_workflow
    name = matter.contact_name or matter.contact_id
    staff = _fsa_staff()
    text = (f"The Letter of Demand to {name} was sent 7 days ago. Please arrange for an "
            f"FSA inspector to deliver it to the facility by hand and get a signed "
            f"acknowledgement of receipt, then upload it on the matter to complete the step.")
    notices = _raise_notices(matter, staff, DebtorNotice.KIND_LOD_DELIVERY_DUE,
                             "Legal workflow", text)
    sent = legal_workflow.lod_served_at(matter)
    _email(f"Action required: hand delivery of the Letter of Demand \u2013 {name}", {
        "paragraphs": [
            format_html("The Letter of Demand to <strong>{}</strong> was sent on {}. As seven days have now "
                        "passed, please arrange for an FSA inspector to deliver it to the facility by hand "
                        "and to obtain a signed acknowledgement of receipt.", name, _when(sent)),
            "Once it has been delivered, please upload the signed acknowledgement on the matter and "
            "confirm the delivery. The step will remain open on the matter until this has been done.",
        ],
        "url": _matter_url(matter), "link_text": "view the matter on the Debtor System",
    }, staff)
    matter.lod_delivery_notified_at = now
    matter.save(update_fields=["lod_delivery_notified_at"])
    return notices


def send_due_lod_delivery_reminders(now=None):
    """Raise the hand-delivery alert for every active matter whose Letter of
    Demand went out at least 7 days ago, is not yet delivered by hand, and has
    not been alerted on. Safe to run as often as you like. Returns how many."""
    from . import legal_workflow
    from .models import LegalMatter
    now = now or timezone.now()
    count = 0
    for m in (LegalMatter.objects.filter(status=LegalMatter.ACTIVE,
                                         lod_delivery_notified_at__isnull=True)
              .prefetch_related("step_states")):
        state = legal_workflow.lod_delivery_state(m, now)
        if state["outstanding"]:
            notify_lod_delivery_due(m, now)
            count += 1
    return count


def notify_lod_delivered(matter, by_user=None, by_name=""):
    """Tell FSA and the legal team the Letter of Demand has been hand-delivered
    and the signed proof is on the matter. ``by_name`` names who confirmed it
    when that was not a user - an inspector through their upload link."""
    from .models import DebtorNotice
    name = matter.contact_name or matter.contact_id
    who = (by_user.get_full_name() or by_user.email) if by_user else by_name
    text = (f"The Letter of Demand to {name} has been delivered by hand. {who} confirmed it "
            f"and uploaded the signed proof of delivery.")
    people = {u.id: u for u in _fsa_staff() + _legal_team() if u.id != getattr(by_user, "id", None)}
    notices = _raise_notices(matter, people.values(), DebtorNotice.KIND_LOD_DELIVERED, who, text)
    _email(f"Letter of Demand delivered \u2013 {name}", {
        "paragraphs": [
            format_html("Please note that the Letter of Demand to <strong>{}</strong> has been delivered to "
                        "the facility by hand. {} confirmed the delivery on {} and uploaded the signed "
                        "acknowledgement of receipt, which is now available on the matter.",
                        name, who, _when(timezone.now())),
        ],
        "url": _matter_url(matter), "link_lead": "You can",
        "link_text": "view the signed acknowledgement on the Debtor System",
    }, list(people.values()) + _lawyer_recipients())
    return notices


# ---- two-way updates on a matter ----------------------------------------------------

def _attorneys():
    """The legal team: everyone with a Lawyer login."""
    return list(User.objects.filter(role=Role.LAWYER, is_active=True))


def notify_matter_update(matter, actor, headline, detail="", files=(), people=None):
    """Tell the other side that a matter moved on, so neither has to keep checking.

    When an attorney comments, uploads or changes a step, FSA (the Super Admins)
    are told; when FSA does, the attorneys are. The person who made the change is
    never told about it. One email for everyone concerned, plus an in-app notice
    each that opens on the matter. ``people`` overrides who is told (a closure
    waiting for approval goes to the Super Admins whoever recommended it).
    Returns the notices raised."""
    from .models import DebtorNotice
    name = matter.contact_name or matter.contact_id
    who = actor.get_full_name() or actor.email
    if people is None:
        people = _fsa_staff() if getattr(actor, "is_lawyer", False) else _matter_attorneys(matter)
    people = [u for u in people if u.id != actor.id]
    if not people:
        return []
    lines = [f"{who} {headline}."]
    if detail:
        lines += ["", detail]
    if files:
        lines += ["", "Documents:"] + [f"  - {f}" for f in files]
    text = "\n".join(lines)
    notices = _raise_notices(matter, people, DebtorNotice.KIND_MATTER_UPDATE, who, text)
    _email(f"Update on the {name} matter", {
        "paragraphs": [
            format_html("Please note that {} {} on the <strong>{}</strong> matter on {}.",
                        who, headline, name, _when(timezone.now())),
        ],
        "quote": detail, "quote_label": "Comment" if detail else "",
        "documents": list(files),
        "url": _matter_url(matter), "link_text": "view the matter on the Debtor System",
    }, people)
    return notices


# ---- the legal diary: payment checks, missed payments, follow-ups, daily list ----

def _matter_attorneys(matter):
    """The attorney responsible for the matter, or with nobody set, all of them."""
    a = getattr(matter, "attorney", None)
    return [a] if a and a.is_active else _attorneys()


def _remind_once(key, matter=None):
    """True the first time a reminder key is seen; it is then recorded, so the
    hourly run never sends the same reminder twice."""
    from .models import LegalReminder
    _obj, created = LegalReminder.objects.get_or_create(key=key[:120], defaults={"matter": matter})
    return created


def _diary_notice(matter, people, subject, paragraphs, text):
    """One in-app notice each and one email each for a diary reminder."""
    from .models import DebtorNotice
    notices = _raise_notices(matter, people, DebtorNotice.KIND_LEGAL_REMINDER, "Legal diary", text)
    _email(subject, {"paragraphs": paragraphs, "url": _matter_url(matter),
                     "link_text": "open the matter on the Debtor System"}, people)
    return notices


def _money(v):
    return f"R {v:,.2f}"


def _long_day(d):
    return f"{d.day} {d:%B %Y}"


def send_legal_diary_reminders(now=None, digest_hour=7):
    """The legal diary's automatic reminders. Safe to run every hour: each goes
    out once.

      * on the date a promised payment falls due (and a couple of days before an
        instalment), the responsible attorney is asked to check it was paid;
      * a payment that has not come in a few days after its date is flagged to
        the attorney and FSA;
      * a follow-up goes to whoever diarised it, on its date;
      * from ``digest_hour`` each morning, every attorney and Super Admin gets one
        email listing what is overdue and due today for them.

    Returns how many reminders went out (counting each digest once)."""
    from . import legal_board, reports
    from .models import LegalMatter, LegalPaymentPlan
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    matters = list(LegalMatter.objects.filter(status=LegalMatter.ACTIVE)
                   .select_related("attorney", "follow_up_user")
                   .prefetch_related("step_states", "payment_plans", "payments", "lod_instructions"))
    payments = {}
    for tenant in {m.tenant_id for m in matters}:
        payments.update(legal_board.payments_by_contact(
            tenant, [m for m in matters if m.tenant_id == tenant]))
    sent = 0
    states = []
    for m in matters:
        days_idle, _grace = reports.idle_days(m, now)
        state = legal_board.board_state(m, now, payments.get(m.id, []), days_idle)
        states.append((m, state))
        name = m.contact_name or m.contact_id
        plan, pr = state["plan"], state["progress"]
        if plan and not pr["paid_in_full"]:
            if pr["behind"]:
                missed = pr["first_missed"]
                n = missed["n"] if missed else 1
                if _remind_once(f"plan:{plan.id}:missed:{n}", m):
                    what = ("the promised payment" if plan.kind == LegalPaymentPlan.PROMISE
                            else f"instalment {n}")
                    due = missed["date"] if missed else plan.first_date
                    people = {u.id: u for u in _matter_attorneys(m) + _fsa_staff()}
                    _diary_notice(m, list(people.values()), f"Payment missed – {name}", [
                        format_html("<strong>{}</strong> has not paid {} that was due on {}. {} has been "
                                    "received since the {} was agreed, and the account is {} behind.",
                                    name, what, _long_day(due), _money(pr["received"]),
                                    plan.get_kind_display().lower(), _money(pr["shortfall"])),
                        "The matter is now flagged as needing action on the Lawyers page. Please follow up "
                        "with the debtor and record the next step.",
                    ], f"Payment missed: {name} has not paid {what} due on {_long_day(due)} "
                       f"({_money(pr['shortfall'])} behind).")
                    sent += 1
            elif pr["next_date"]:
                lead = 0 if plan.kind == LegalPaymentPlan.PROMISE else legal_board.INSTALMENT_REMINDER_DAYS
                if (pr["next_date"] - timedelta(days=lead) <= today
                        and _remind_once(f"plan:{plan.id}:check:{pr['next_n']}", m)):
                    what = ("The promised payment" if plan.kind == LegalPaymentPlan.PROMISE
                            else f"Instalment {pr['next_n']}")
                    when = pr["next_date"]
                    _diary_notice(m, _matter_attorneys(m), f"Payment due – {name}", [
                        format_html("{} of {} from <strong>{}</strong> is due on {}.",
                                    what, _money(pr["next_amount"]), name, _long_day(when)),
                        "Please check that it has been received. Payments into FSA's account show on the "
                        "matter once Xero has them; a payment made to you can be recorded on the matter.",
                    ], f"Payment due: {what.lower()} of {_money(pr['next_amount'])} from {name} "
                       f"on {_long_day(when)}.")
                    sent += 1
        if (m.follow_up_on and m.follow_up_on <= today
                and _remind_once(f"follow_up:{m.id}:{m.follow_up_on.isoformat()}", m)):
            people = ([m.follow_up_user] if m.follow_up_user and m.follow_up_user.is_active
                      else _matter_attorneys(m))
            note = m.follow_up_note or "a follow-up on this matter"
            _diary_notice(m, people, f"Follow-up due – {name}", [
                format_html("This is your reminder to follow up on <strong>{}</strong>: {}.", name, note),
            ], f"Follow-up due on {name}: {note}")
            sent += 1
        sent += _lod_instruction_reminders(m, today)

    if timezone.localtime(now).hour >= digest_hour:
        sent += _send_diary_digests(states, today)
    return sent


def _send_diary_digests(states, today):
    """Each attorney's and Super Admin's list for the day: what is overdue and
    what falls due today on their matters. Nothing to report, no email."""
    from . import legal_board
    people = {u.id: u for u in _attorneys() + _fsa_staff()}
    sent = 0
    for user in people.values():
        lines = []
        for m, state in states:
            mine = user.is_lawyer and m.attorney_id in (None, user.id)
            for t in state["tasks"]:
                if not (t["flag"] or t["due"] == today):
                    continue
                fsa_task = t["owner"] == legal_board.OWNER_FSA
                if (fsa_task and user.is_super_admin) or (not fsa_task and mine):
                    when = "overdue" if t["overdue"] else "today"
                    lines.append(f"{m.contact_name or m.contact_id}: {t['text']} ({when})")
        if not lines or not user.email or not _remind_once(f"digest:{user.id}:{today.isoformat()}"):
            continue
        n = len(lines)
        _email(f"Your legal diary for {_long_day(today)}", {
            "paragraphs": [f"There {'is' if n == 1 else 'are'} {n} item{'' if n == 1 else 's'} on the "
                           "legal matters that need your attention today."],
            "documents": lines, "list_label": "Today's list",
            "url": settings.SITE_BASE_URL + reverse("xero_legal_diary"),
            "link_lead": "You can", "link_text": "open the legal diary on the Debtor System",
        }, [user])
        sent += 1
    return sent


def _lod_instruction_reminders(matter, today):
    """For an inspector's open hand-delivery instruction: a reminder to the
    inspector a couple of days before the deadline, and once it has passed,
    another to the inspector plus an alert to whoever sent it and FSA."""
    from . import lod_dispatch
    sent = 0
    name = matter.contact_name or matter.contact_id
    for ins in matter.lod_instructions.all():
        if ins.status != "sent":
            continue
        soon = ins.due_on - timedelta(days=lod_dispatch.REMIND_DAYS_BEFORE)
        if soon <= today <= ins.due_on and _remind_once(f"lodins:{ins.id}:soon", matter):
            try:
                lod_dispatch.remind_inspector(ins, overdue=False)
                sent += 1
            except Exception:  # noqa: BLE001 - a reminder must never stop the run
                logger.exception("LOD reminder to %s failed", ins.to_email)
        if ins.due_on < today and _remind_once(f"lodins:{ins.id}:overdue", matter):
            try:
                lod_dispatch.remind_inspector(ins, overdue=True)
            except Exception:  # noqa: BLE001
                logger.exception("Overdue LOD reminder to %s failed", ins.to_email)
            people = {u.id: u for u in _fsa_staff()}
            if ins.sent_by and ins.sent_by.is_active:
                people[ins.sent_by.id] = ins.sent_by
            due = ins.due_on
            _diary_notice(matter, list(people.values()), f"Hand delivery overdue – {name}", [
                format_html("{} was asked on {} to deliver the Letter of Demand to <strong>{}</strong> by {}, and "
                            "the signed proof of delivery has not come in. The inspector has been sent a "
                            "reminder.", ins.inspector_name, _when(ins.sent_at), name, _long_day(due)),
                "If the delivery could not be made, please record it as unsuccessful on the matter.",
            ], f"Hand delivery overdue: {ins.inspector_name} was to deliver the LOD to {name} by "
               f"{_long_day(due)}; no signed proof yet.")
            sent += 1
    return sent
