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
    recipients = sorted(set(_lawyer_recipients()) | {u.email for u in attorneys if u.email})
    if not recipients:
        logger.info("New-matter approval for %s: no lawyer/recipient emails to notify.",
                    matter.contact_name or matter.contact_id)
        return 0

    name = matter.contact_name or matter.contact_id
    ctx = {
        "name": name,
        "approved_by": approved_by or matter.approved_by or "",
        "approved_at": matter.approved_at or timezone.now(),
        "url": settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id]),
        "legal_url": settings.SITE_BASE_URL + reverse("xero_legal"),
    }
    subject = f"New client for the lawyers — {name} needs attention"
    text_body = render_to_string("xero/new_matter_email.txt", ctx)
    html_body = render_to_string("xero/new_matter_email.html", ctx)

    to = [settings.MS_GRAPH_SENDER] if settings.MS_GRAPH_SENDER else [recipients[0]]
    try:
        return send_app_email(subject=subject, body=text_body, to=to,
                              html_body=html_body, bcc=recipients)
    except Exception:
        logger.exception("Failed to send new-matter approval notification for %s", name)
        return 0


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


def _email(subject, body, recipients):
    recipients = sorted({e for e in recipients if e})
    if not recipients:
        return 0
    to = [settings.MS_GRAPH_SENDER] if settings.MS_GRAPH_SENDER else [recipients[0]]
    try:
        return send_app_email(subject=subject, body=body, to=to, bcc=recipients)
    except Exception:
        logger.exception("Failed to send '%s'", subject)
        return 0


def notify_lod_delivery_due(matter, now=None):
    """Tell FSA the Letter of Demand is due for hand delivery by an inspector.
    Sent once per matter; returns the notices raised."""
    from .models import DebtorNotice
    now = now or timezone.now()
    name = matter.contact_name or matter.contact_id
    url = settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id])
    staff = _fsa_staff()
    text = (f"The Letter of Demand to {name} was sent 7 days ago. Please arrange for an "
            f"FSA inspector to deliver it to the facility by hand and get a signed "
            f"acknowledgement of receipt, then upload it on the matter to complete the step.")
    notices = _raise_notices(matter, staff, DebtorNotice.KIND_LOD_DELIVERY_DUE,
                             "Legal workflow", text)
    _email(f"Hand-deliver the Letter of Demand - {name}", f"{text}\n\nOpen the matter: {url}\n",
           [u.email for u in staff])
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
    url = settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id])
    text = (f"The Letter of Demand to {name} has been delivered by hand. {who} confirmed it "
            f"and uploaded the signed proof of delivery.")
    people = {u.id: u for u in _fsa_staff() + _legal_team() if u.id != by_user.id}
    notices = _raise_notices(matter, people.values(), DebtorNotice.KIND_LOD_DELIVERED, who, text)
    _email(f"Letter of Demand delivered - {name}", f"{text}\n\nOpen the matter: {url}\n",
           [u.email for u in people.values()] + _lawyer_recipients())
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
    url = settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id])
    _email(f"{name}: {headline} ({who})", f"Matter: {name}\n\n{text}\n\nOpen the matter: {url}\n",
           [u.email for u in people])
    return notices
