"""Weekly lawyer progress report — data assembly, PDF, and sending.

Used by both the scheduled management command (`send_lawyer_report`) and the
"Send now" button on the report settings page, so the content is identical
however it's triggered. The email carries the headline KPIs and a link to the
site; the detailed, severity-coloured breakdown is attached as a PDF
(xero_app.report_pdf). Sent through the Graph email path (xero_app.mailer).

Pending-approval matters are intentionally excluded — that's an administrator
concern, not something the lawyers' report should surface.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from . import legal_workflow
from .mailer import send_app_email
from .models import (LawyerReportConfig, LegalMatter, OpenInvoiceSnapshot,
                     RecoveredInvoice, ReportRecipient)
from .report_pdf import build_report_pdf

# Litigation = any step past the Collections phase.
_LITIGATION_KEYS = legal_workflow.ALL_STEP_KEYS - {t[0] for t in legal_workflow.COLLECTIONS}


def _last_activity(matter):
    """The most recent date anything happened on a matter — mirrors the last
    entry of the matter timeline (handover, approval, completed steps, comments,
    closure). Falls back to the handover date."""
    dates = [matter.sent_at, matter.approved_at, matter.closed_at]
    dates += [s.done_at for s in matter.step_states.all() if s.done and s.done_at]
    dates += [c.created_at for c in matter.step_comments.all()]
    dates = [d for d in dates if d]
    return max(dates) if dates else matter.sent_at


def _severity(days_idle):
    if days_idle >= 14:
        return "critical"
    if days_idle >= 7:
        return "warning"
    return "ok"


def _owed_by_matter(tenant_id, matters):
    """What each matter's company still owes FSA today, keyed by matter id.

    Resolves on contact_id with a contact_name fallback, the same way the Lawyers
    page does, so the "Owes" figure on a card and in the report reconcile.
    """
    ids = [m.contact_id for m in matters if m.contact_id]
    by_cid = {r["contact_id"]: r["s"] for r in
              OpenInvoiceSnapshot.objects
              .filter(tenant_id=tenant_id, contact_id__in=ids)
              .values("contact_id").annotate(s=Sum("amount_due"))} if ids else {}
    names = [m.contact_name for m in matters
             if m.contact_name and m.contact_id not in by_cid]
    by_name = {r["contact_name"]: r["s"] for r in
               OpenInvoiceSnapshot.objects
               .filter(tenant_id=tenant_id, contact_name__in=names)
               .values("contact_name").annotate(s=Sum("amount_due"))} if names else {}
    return {m.id: (by_cid.get(m.contact_id) or by_name.get(m.contact_name) or Decimal(0))
            for m in matters}


def _stage_label(matter, in_litigation):
    """One line for where the matter stands. Collections matters say just that —
    the Unopposed/Opposed routes have no bearing until litigation starts, so
    spelling them out on every row would be noise."""
    if not in_litigation:
        return "Collections"
    return ("Litigation - Summons %s, Application %s" % (
        "Opposed" if matter.summons_opposed else "Unopposed",
        "Opposed" if matter.application_opposed else "Unopposed"))


def _matter_summary(matter, now, owed=Decimal(0)):
    visible = legal_workflow.visible_step_keys(matter.summons_opposed, matter.application_opposed)
    done_keys = {s.step_key for s in matter.step_states.all() if s.done}
    done = len([k for k in visible if k in done_keys])
    total = len(visible)
    last = _last_activity(matter)
    days_idle = (now - last).days if last else 0
    in_litigation = bool(done_keys & _LITIGATION_KEYS)
    return {
        "id": matter.id,
        "url": settings.SITE_BASE_URL + reverse("xero_legal_matter", args=[matter.id]),
        "name": matter.contact_name or matter.contact_id,
        "status": matter.get_status_display(),
        "status_key": matter.status,
        "sent_at": matter.sent_at,
        "sent_by": matter.sent_by,
        "approved_at": matter.approved_at,
        "amount_owed": owed,
        "done": done,
        "total": total,
        "pct": round(done / total * 100) if total else 0,
        "route_summary": (f"Summons: {'Opposed' if matter.summons_opposed else 'Unopposed'} "
                          f"· Application: {'Opposed' if matter.application_opposed else 'Unopposed'}"),
        "stage_label": _stage_label(matter, in_litigation),
        "in_litigation": in_litigation,
        # What the attorneys last ticked off — same rule as the Lawyers page card.
        "last_action": legal_workflow.last_action(matter),
        "last_worked": last,
        "days_idle": days_idle,
        "severity": _severity(days_idle),
    }


def build_lawyer_report(tenant_id, period_days=7, now=None):
    """Assemble the report context: KPIs, companies newly handed over in the
    period, and every active matter with its progress and last-worked date.
    Pending matters are excluded (admin-only)."""
    now = now or timezone.now()
    period_start = now - timedelta(days=period_days)
    matters = list(LegalMatter.objects.filter(tenant_id=tenant_id)
                   .prefetch_related("step_states", "step_comments"))
    owed = _owed_by_matter(tenant_id, matters)

    active, new_matters, closed_period = [], [], 0
    for m in matters:
        if m.status == LegalMatter.CLOSED:
            if m.closed_at and m.closed_at >= period_start:
                closed_period += 1
            continue
        if m.status != LegalMatter.ACTIVE:
            continue  # pending approval is an administrator concern — never shown here
        summary = _matter_summary(m, now, owed.get(m.id, Decimal(0)))
        active.append(summary)
        if m.sent_at and m.sent_at >= period_start:
            new_matters.append(summary)

    n = len(active)
    in_litigation = sum(1 for s in active if s["in_litigation"])
    idle_7 = sum(1 for s in active if s["days_idle"] >= 7)
    idle_14 = sum(1 for s in active if s["days_idle"] >= 14)
    avg_completion = round(sum(s["pct"] for s in active) / n) if n else 0
    avg_days_idle = round(sum(s["days_idle"] for s in active) / n) if n else 0

    rec = RecoveredInvoice.objects.filter(
        tenant_id=tenant_id, credited=True, reason=RecoveredInvoice.REASON_COLLECTED_LEGAL)
    recovered_total = rec.aggregate(s=Sum("amount"))["s"] or 0
    recovered_period = (rec.filter(recovered_at__gte=period_start)
                        .aggregate(s=Sum("amount"))["s"] or 0)

    active.sort(key=lambda s: (-s["days_idle"], s["pct"]))   # most urgent first
    new_matters.sort(key=lambda s: s["sent_at"] or now, reverse=True)

    # The report reads worst-first, so the matters are banded by how long they have
    # been sitting. Each band carries its own count and value, which is what makes
    # a 50-row list actionable: "42 matters worth R X have not moved in a fortnight".
    groups = [{"key": key, "label": label, "matters": [s for s in active if s["severity"] == key]}
              for key, label in (("critical", "Critical - 14+ days idle"),
                                 ("warning", "Warning - 7 to 13 days idle"),
                                 ("ok", "On track - worked in the last 7 days"))]
    for g in groups:
        g["count"] = len(g["matters"])
        g["owed"] = sum((s["amount_owed"] for s in g["matters"]), Decimal(0))

    kpis = {
        "active": n,
        "new": len(new_matters),
        "closed_period": closed_period,
        "in_litigation": in_litigation,
        "in_collections": n - in_litigation,
        "idle_7": idle_7,
        "idle_14": idle_14,
        "avg_completion": avg_completion,
        "avg_days_idle": avg_days_idle,
        "recovered_total": recovered_total,
        "recovered_period": recovered_period,
        # Value still in the lawyers' hands, and the slice of it that has gone
        # quiet — the two numbers the report exists to put in front of someone.
        "owed_active": sum((s["amount_owed"] for s in active), Decimal(0)),
        "owed_idle_14": sum((s["amount_owed"] for s in active if s["days_idle"] >= 14), Decimal(0)),
    }
    return {
        "generated_at": now,
        "period_days": period_days,
        "period_start": period_start,
        "active_matters": active,
        "severity_groups": [g for g in groups if g["count"]],
        "new_matters": new_matters,
        "kpis": kpis,
        "tiles": [("Active", n), ("New", len(new_matters)),
                  ("Avg done", f"{avg_completion}%"), ("Idle 14+", idle_14)],
        "site_url": settings.SITE_BASE_URL,
        "legal_url": settings.SITE_BASE_URL + reverse("xero_legal"),
    }


def send_lawyer_report(tenant_id, recipients=None, now=None, mark_sent=True, period_days=None):
    """Build and email the lawyer report (KPI email body + detailed PDF attached).
    Returns (messages_sent, context). Recipients are BCC'd (system mailbox as the
    visible To) so the recipient list stays private."""
    cfg = LawyerReportConfig.get_solo()
    now = now or timezone.now()
    period_days = period_days or cfg.period_days()
    ctx = build_lawyer_report(tenant_id, period_days=period_days, now=now)

    if recipients is None:
        recipients = list(ReportRecipient.objects.filter(is_active=True)
                          .values_list("email", flat=True))
    if not recipients:
        return 0, ctx

    subject = f"Lawyer progress report — {timezone.localtime(now).strftime('%d %b %Y')}"
    text_body = render_to_string("xero/lawyer_report_email.txt", ctx)
    html_body = render_to_string("xero/lawyer_report_email.html", ctx)
    pdf = build_report_pdf(ctx)
    filename = f"lawyer-report-{timezone.localtime(now).strftime('%Y-%m-%d')}.pdf"

    to = [settings.MS_GRAPH_SENDER] if settings.MS_GRAPH_SENDER else [recipients[0]]
    sent = send_app_email(subject=subject, body=text_body, to=to, html_body=html_body,
                          bcc=recipients, attachments=[(filename, pdf, "application/pdf")])
    if mark_sent and sent:
        cfg.last_sent_at = now
        cfg.save(update_fields=["last_sent_at"])
    return sent, ctx
