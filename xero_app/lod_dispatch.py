"""Sending a Letter of Demand to an inspector for hand delivery, and following
it through to the signed proof.

FSA picks the inspector (suggested from the debtor's Xero invoices), checks a
pre-written email and the attachments - the attorneys' Letter of Demand and
cover letter, the invoices as Xero prints them, and a statement of account -
and sends it from the system. Each instruction is recorded with who sent it,
when, to whom, what was attached and the deadline. The email carries a private
link through which the inspector uploads the signed acknowledgement of receipt,
which completes the matter's hand-delivery step.
"""
import json
import mimetypes
import re
import secrets
from datetime import timedelta

from django.template.loader import render_to_string
from django.utils import timezone

from . import legal_workflow, notifications
from .mailer import send_app_email
from .models import (ContactDetail, Inspector, LegalClientInfo, LegalStep, LegalStepComment,
                     LegalStepCommentAttachment, LodInstruction, OpenInvoiceSnapshot, XeroConnection)
from .statement_pdf import build_statement_pdf, statement_filename

# The inspector has this long to deliver, unless FSA sets another date.
DUE_DAYS = 7
# A reminder goes to the inspector this many days before the deadline.
REMIND_DAYS_BEFORE = 2
# Microsoft Graph takes about 4 MB per message once the files are encoded;
# this leaves room for that.
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024

# Where the attorneys put the Letter of Demand, and how to tell it (and its
# cover letter) from the proofs of service, read receipts and saved emails that
# sit on the same steps.
LOD_STEPS = ("draft_lod", "lod_email", "lod_sheriff")
_LOD_WORDS = re.compile(r"letter\s*of\s*demand|\blod\b|cover|annexure", re.I)
_NOT_LOD_WORDS = re.compile(r"receipt|\bpos\b|\bpons\b|proof|non[- ]?service|notification|reply|\bre\b", re.I)
_DOC_TYPES = (".pdf", ".doc", ".docx")


class DispatchError(Exception):
    """The instruction could not be sent; the message says why."""


# ---- inspectors -----------------------------------------------------------------------

def clean_inspector(raw):
    """'020 - S Dlisani' -> 'S Dlisani'."""
    return re.sub(r"^\s*\d+\s*-\s*", "", raw or "").strip()


def inspector_names(raw):
    """The inspector field may name several, comma separated."""
    return [n for n in (clean_inspector(p) for p in (raw or "").split(",")) if n]


def ensure_inspectors(names):
    """Make sure each name has an address-book entry."""
    have = set(Inspector.objects.filter(name__in=names).values_list("name", flat=True))
    Inspector.objects.bulk_create([Inspector(name=n) for n in sorted(set(names) - have)],
                                  ignore_conflicts=True)


def sync_inspectors_from_xero(tenant_id):
    """Add every inspector named on an open invoice to the address book."""
    names = set()
    for raw in (OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id).exclude(inspector="")
                .values_list("inspector", flat=True).distinct()):
        names.update(inspector_names(raw))
    ensure_inspectors(names)
    return names


# ---- what the instruction is about ---------------------------------------------------

def open_invoices(tenant_id, matter):
    qs = OpenInvoiceSnapshot.objects.filter(tenant_id=tenant_id)
    rows = list(qs.filter(contact_id=matter.contact_id).order_by("due_date")) if matter.contact_id else []
    if not rows and matter.contact_name:
        rows = list(qs.filter(contact_name=matter.contact_name).order_by("due_date"))
    return rows


def suggested_inspectors(invoices):
    """The inspectors on the debtor's invoices, the one on most invoices first."""
    counts = {}
    for inv in invoices:
        for n in inspector_names(inv.inspector):
            counts[n] = counts.get(n, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n))


def matter_documents(matter):
    """Every document on the matter, newest first, with the Letter of Demand
    and its cover letter ticked: [{"id", "name", "step", "size", "url", "picked"}]."""
    out = []
    for a in (LegalStepCommentAttachment.objects.filter(comment__matter=matter)
              .select_related("comment").order_by("-uploaded_at")):
        name = a.original_name or a.file.name.rsplit("/", 1)[-1]
        try:
            size = a.file.size
        except (OSError, ValueError):
            continue          # the file is no longer on disk
        step = a.comment.step_key
        picked = (step in LOD_STEPS and name.lower().endswith(_DOC_TYPES)
                  and bool(_LOD_WORDS.search(name)) and not _NOT_LOD_WORDS.search(name))
        out.append({"id": a.id, "name": name, "size": size, "url": a.file.url, "picked": picked,
                    "step": legal_workflow.STEP_LABELS.get(step, "Status & payments" if step == "_status" else step)})
    return out


def debtor_address(tenant_id, matter):
    """Where to deliver: the domicilium captured for the proceedings, else the
    street address Xero has. Returns (lines, contact person, phone)."""
    info = LegalClientInfo.objects.filter(matter=matter).first()
    cached = ContactDetail.objects.filter(tenant_id=tenant_id, contact_id=matter.contact_id or matter.contact_name).first()
    data = json.loads(cached.data_json or "{}") if cached else {}
    street = next((a.get("lines") or [] for a in data.get("addresses") or []
                   if (a.get("type") or "").lower() == "street" and a.get("lines")), [])
    lines = [l.strip() for l in (info.domicilium_address.splitlines() if info and info.domicilium_address
                                 else street) if l.strip()]
    person = (info.contact_person if info else "") or next(
        (p.get("name") for p in data.get("contact_persons") or [] if p.get("name")), "")
    phones = data.get("phones") or []
    phone = (info.contact_phone if info else "") or (phones[0].get("number") if phones else "")
    return lines, person, phone


def default_subject(matter):
    return f"Hand delivery of a Letter of Demand - {matter.contact_name or matter.contact_id}"


def default_body(matter, inspector_name, address_lines, person, phone, due_on, sender_name):
    name = matter.contact_name or matter.contact_id
    where = ("\n".join(address_lines) if address_lines
             else "(no address on record - please confirm the facility's location)")
    contact = ", ".join(x for x in (person, phone) if x)
    return (
        f"Dear {inspector_name},\n\n"
        f"Please deliver the attached Letter of Demand to {name} by hand, by {due_on.day} {due_on:%B %Y}.\n\n"
        f"Address:\n{where}\n"
        + (f"\nContact at the facility: {contact}\n" if contact else "")
        + "\nPlease hand the documents to the owner or manager and ask them to sign an acknowledgement "
          "of receipt with their name, signature, the date and the time. Then upload a photo or scan of "
          "the signed acknowledgement using the link below.\n\n"
          "If the facility cannot be found, is closed, or refuses to accept the documents, please reply "
          "to this email and let me know.\n\n"
        f"Kind regards,\n{sender_name}\nFood Safety Agency"
    )


# ---- sending ------------------------------------------------------------------------

def collect_attachments(tenant_id, matter, doc_ids, invoice_ids, with_statement, invoices=None):
    """The files to send: [(filename, bytes, mimetype)] and their summary
    [{"name", "kind", "bytes"}]. Raises DispatchError when something cannot be
    fetched, or the whole is too large to email."""
    files = []
    docs = list(LegalStepCommentAttachment.objects.filter(comment__matter=matter, id__in=doc_ids))
    # The cover letter first, then the rest in the order they were uploaded.
    docs.sort(key=lambda a: ("cover" not in (a.original_name or "").lower(), a.uploaded_at, a.id))
    for a in docs:
        name = a.original_name or a.file.name.rsplit("/", 1)[-1]
        try:
            with a.file.open("rb") as fh:
                data = fh.read()
        except OSError:
            raise DispatchError(f'The document "{name}" could not be read. Untick it or upload it again.')
        files.append((name, data, mimetypes.guess_type(name)[0] or "application/octet-stream", "document"))
    invoices = invoices if invoices is not None else open_invoices(tenant_id, matter)
    chosen = [i for i in invoices if i.invoice_id in set(invoice_ids)]
    if chosen:
        from .xero_client import fetch_invoice_pdf
        conn = XeroConnection.objects.filter(tenant_id=tenant_id).first()
        if not conn:
            raise DispatchError("Not connected to Xero, so the invoices cannot be attached.")
        for inv in chosen:
            try:
                pdf = fetch_invoice_pdf(conn, inv.invoice_id)
            except Exception as exc:  # noqa: BLE001 - any Xero failure stops the send
                raise DispatchError(f"Xero did not return invoice {inv.invoice_number}: {exc}")
            files.append((f"Invoice {inv.invoice_number or inv.invoice_id}.pdf", pdf, "application/pdf", "invoice"))
    if with_statement:
        lines, _person, _phone = debtor_address(tenant_id, matter)
        name = matter.contact_name or matter.contact_id
        files.append((statement_filename(name), build_statement_pdf(name, lines, invoices),
                      "application/pdf", "statement"))
    if not files:
        raise DispatchError("Choose at least one document to send.")
    total = sum(len(f[1]) for f in files)
    if total > MAX_ATTACHMENT_BYTES:
        raise DispatchError(f"The attachments come to {total / 1048576:.1f} MB; email allows about "
                            f"{MAX_ATTACHMENT_BYTES / 1048576:.0f} MB. Untick the largest document or some invoices.")
    return ([(n, d, m) for n, d, m, _k in files],
            [{"name": n, "kind": k, "bytes": len(d)} for n, d, m, k in files])


def _render(instruction):
    ctx = {"paragraphs": [p.strip() for p in instruction.body.split("\n\n") if p.strip()],
           "upload_url": instruction.upload_url, "due_on": instruction.due_on,
           "attachments": [a["name"] for a in instruction.attachments]}
    return (render_to_string("xero/lod_instruction_email.txt", ctx),
            render_to_string("xero/lod_instruction_email.html", ctx))


def send_instruction(*, user, matter, inspector, to_email, cc, subject, body, due_on,
                     files, summary, upload_url_for):
    """Email the instruction and record it. ``upload_url_for(token)`` gives the
    inspector's upload address. Returns the LodInstruction; raises DispatchError
    if the email could not be sent (nothing is recorded then)."""
    who = user.get_full_name() or user.email
    token = secrets.token_urlsafe(32)
    instruction = LodInstruction(
        matter=matter, inspector=inspector, inspector_name=inspector.name, to_email=to_email,
        cc=", ".join(cc), subject=subject[:255], body=body, attachments=summary,
        sent_by=user, sent_by_name=who, due_on=due_on, token=token, upload_url=upload_url_for(token))
    text, html = _render(instruction)
    try:
        sent = send_app_email(subject=instruction.subject, body=text, html_body=html, to=[to_email],
                              cc=list(cc) or None, reply_to=[user.email] if user.email else None,
                              attachments=files)
    except Exception as exc:  # noqa: BLE001
        raise DispatchError(f"The email could not be sent: {exc}")
    if not sent:
        raise DispatchError("The email could not be sent. Please try again.")
    instruction.save()

    # The audit trail, on the hand-delivery step where everyone looks for it.
    names = ", ".join(a["name"] for a in summary)
    LegalStepComment.objects.create(
        matter=matter, step_key=legal_workflow.LOD_HAND_DELIVERY, author=user, author_name=who,
        text=(f"Hand-delivery instruction emailed to {inspector.name} ({to_email}), to deliver by "
              f"{due_on:%d %b %Y}." + (f" Copied to {', '.join(cc)}." if cc else "") + f"\nAttached: {names}."))
    notifications.notify_matter_update(
        matter, user, f"instructed {inspector.name} to hand-deliver the Letter of Demand by {due_on:%d %b %Y}")
    return instruction


def remind_inspector(instruction, overdue):
    """Email the inspector the instruction again, with the same upload link."""
    first = "This is a reminder about the hand delivery below, which " + (
        "was due on" if overdue else "is due by") + f" {instruction.due_on.day} {instruction.due_on:%B %Y}."
    copy = LodInstruction(body=first + "\n\n" + instruction.body, upload_url=instruction.upload_url,
                          due_on=instruction.due_on, attachments=instruction.attachments)
    text, html = _render(copy)
    cc = [e.strip() for e in instruction.cc.split(",") if e.strip()]
    if instruction.sent_by and instruction.sent_by.email:
        cc.append(instruction.sent_by.email)
    return send_app_email(subject=("Overdue: " if overdue else "Reminder: ") + instruction.subject,
                          body=text, html_body=html, to=[instruction.to_email], cc=cc or None,
                          reply_to=[instruction.sent_by.email] if instruction.sent_by and instruction.sent_by.email else None)


# ---- completing --------------------------------------------------------------------

def close_open_instructions(matter, status, by_name, note=""):
    """Close whatever instruction is still open on the matter."""
    return LodInstruction.objects.filter(matter=matter, status=LodInstruction.SENT).update(
        status=status, closed_by=by_name, closed_at=timezone.now(), outcome_note=note[:500])


def complete_from_inspector(instruction, uploaded_file, note=""):
    """The inspector uploaded the signed acknowledgement through their link:
    file it on the hand-delivery step, tick the step, close the instruction and
    tell FSA and the legal team."""
    matter = instruction.matter
    who = f"{instruction.inspector_name} (inspector)"
    comment = LegalStepComment.objects.create(
        matter=matter, step_key=legal_workflow.LOD_HAND_DELIVERY, author=None, author_name=who,
        text=("Signed proof of delivery uploaded by the inspector." + (f"\n{note}" if note else "")))
    LegalStepCommentAttachment.objects.create(comment=comment, file=uploaded_file,
                                              original_name=uploaded_file.name[:255],
                                              nature="Signed proof of delivery")
    now = timezone.now()
    step, _ = LegalStep.objects.get_or_create(matter=matter, step_key=legal_workflow.LOD_HAND_DELIVERY)
    if not step.done:
        step.done, step.done_by, step.done_at = True, who, now
        step.save()
    instruction.status, instruction.closed_by, instruction.closed_at = LodInstruction.DELIVERED, who, now
    instruction.outcome_note = note[:500]
    instruction.save(update_fields=["status", "closed_by", "closed_at", "outcome_note"])
    if matter.delivery_failed:
        matter.delivery_failed = False
        matter.save(update_fields=["delivery_failed", "updated_at"])
    notifications.notify_lod_delivered(matter, by_name=instruction.inspector_name + " (inspector)")
    return comment


def link_is_usable(instruction):
    """An upload link works while the instruction is open, and for 60 days."""
    return instruction.is_open and instruction.sent_at >= timezone.now() - timedelta(days=60)
