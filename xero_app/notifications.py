"""Transactional lawyer notifications (distinct from the weekly digest in
reports.py). Currently: alert the lawyers when a new matter is approved and
becomes active, so they know a new client needs attention.

Sends through the same Graph email path as everything else. Any send failure is
logged and swallowed — a notification must never block the approval itself.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
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
    """The attorneys, plus anyone given working access to the Lawyers page."""
    return list(User.objects.filter(is_active=True)
                .filter(models.Q(role=Role.LAWYER) | models.Q(legal_access=True)))


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


def notify_lod_delivered(matter, by_user):
    """Tell FSA and the legal team the Letter of Demand has been hand-delivered
    and the signed proof is on the matter."""
    from .models import DebtorNotice
    name = matter.contact_name or matter.contact_id
    who = by_user.get_full_name() or by_user.email
    text = (f"The Letter of Demand to {name} has been delivered by hand. {who} confirmed it "
            f"and uploaded the signed proof of delivery.")
    people = {u.id: u for u in _fsa_staff() + _legal_team() if u.id != by_user.id}
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


def notify_matter_update(matter, actor, headline, detail="", files=()):
    """Tell the other side that a matter moved on, so neither has to keep checking.

    When an attorney comments, uploads or changes a step, FSA (the Super Admins)
    are told; when FSA does, the attorneys are. The person who made the change is
    never told about it. One email for everyone concerned, plus an in-app notice
    each that opens on the matter. Returns the notices raised."""
    from .models import DebtorNotice
    name = matter.contact_name or matter.contact_id
    who = actor.get_full_name() or actor.email
    people = _fsa_staff() if getattr(actor, "is_lawyer", False) else _attorneys()
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
