"""A debtor's statement of account as a one-or-more page PDF: every open
invoice with its dates and what is still owed, the total, and how old the debt
is. Sent with a Letter of Demand for hand delivery, so the facility sees exactly
what the demand is for.

Uses reportlab, like the lawyer report. ASCII-only punctuation so Helvetica
renders everything.
"""
import io
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COMPANY = "Food Safety Agency (Pty) Ltd"
BRAND = colors.HexColor("#0E7C7B")
NAVY = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
GRID = colors.HexColor("#e5e7eb")
STRIPE = colors.HexColor("#f8fafc")

# Ageing columns, by days past the due date.
AGES = [("Current", None, 0), ("1-30 days", 1, 30), ("31-60 days", 31, 60),
        ("61-90 days", 61, 90), ("91-120 days", 91, 120), ("Over 120 days", 121, None)]


def _money(v):
    return "R {:,.2f}".format(float(v or 0))


def _day(d):
    return d.strftime("%d %b %Y") if d else "-"


def _age(days):
    days = days or 0
    for label, lo, hi in AGES:
        if lo is None and days <= 0:
            return label
        if lo is not None and days >= lo and (hi is None or days <= hi):
            return label
    return AGES[-1][0]


def build_statement_pdf(debtor_name, address_lines, invoices, as_of=None):
    """``invoices``: OpenInvoiceSnapshot rows (or anything with invoice_number,
    invoice_date, due_date, days_past_due, total and amount_due). Returns bytes."""
    as_of = as_of or timezone.localdate()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Statement of account - {debtor_name}", author=COMPANY)
    base = getSampleStyleSheet()["Normal"]
    st = {
        "title": ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=16, textColor=NAVY,
                                alignment=TA_RIGHT, leading=20),
        "sub": ParagraphStyle("s", parent=base, fontSize=9, textColor=MUTED, alignment=TA_RIGHT, leading=12),
        "label": ParagraphStyle("l", parent=base, fontName="Helvetica-Bold", fontSize=8, textColor=MUTED,
                                leading=11, spaceAfter=2),
        "body": ParagraphStyle("b", parent=base, fontSize=10, textColor=NAVY, leading=13),
        "cell": ParagraphStyle("c", parent=base, fontSize=9, textColor=NAVY, leading=11),
        "foot": ParagraphStyle("f", parent=base, fontSize=8, textColor=MUTED, leading=10),
    }

    logo_path = Path(settings.BASE_DIR) / "static" / "img" / "fsa-logo.png"
    logo = Image(str(logo_path), width=42 * mm, height=16 * mm, kind="proportional") if logo_path.exists() else ""
    head = Table([[logo, [Paragraph("STATEMENT OF ACCOUNT", st["title"]),
                          Paragraph(f"As at {_day(as_of)}", st["sub"])]]],
                 colWidths=[70 * mm, None])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    to_lines = [f"<b>{escape(debtor_name)}</b>"] + [escape(l) for l in address_lines if l]
    parties = Table([[[Paragraph("FROM", st["label"]), Paragraph(f"<b>{COMPANY}</b>", st["body"])],
                      [Paragraph("TO", st["label"]), Paragraph("<br/>".join(to_lines), st["body"])]]],
                    colWidths=[None, None])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    rows = [["Invoice", "Invoice date", "Due date", "Days overdue", "Invoice total", "Amount due"]]
    total = 0
    by_age = {label: 0 for label, _lo, _hi in AGES}
    for inv in sorted(invoices, key=lambda i: (i.due_date or as_of)):
        total += float(inv.amount_due or 0)
        by_age[_age(inv.days_past_due)] += float(inv.amount_due or 0)
        rows.append([inv.invoice_number or "-", _day(inv.invoice_date), _day(inv.due_date),
                     str(max(0, inv.days_past_due or 0)), _money(inv.total), _money(inv.amount_due)])
    rows.append(["", "", "", "", "Total due", _money(total)])
    widths = [32 * mm, 26 * mm, 26 * mm, 24 * mm, 30 * mm, None]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("TEXTCOLOR", (0, 1), (-1, -1), NAVY),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, STRIPE]),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, GRID),
        ("FONT", (4, -1), (-1, -1), "Helvetica-Bold", 10),
        ("LINEABOVE", (4, -1), (-1, -1), 1, NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    ages = Table([[label for label, _lo, _hi in AGES], [_money(by_age[label]) for label, _lo, _hi in AGES]],
                 colWidths=[None] * len(AGES))
    ages.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("FONT", (0, 1), (-1, 1), "Helvetica", 9.5), ("TEXTCOLOR", (0, 1), (-1, 1), NAVY),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), STRIPE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    story = [head, Spacer(1, 8 * mm), parties, Spacer(1, 8 * mm), table, Spacer(1, 8 * mm),
             Paragraph("AGE OF THE AMOUNT DUE", st["label"]), ages, Spacer(1, 10 * mm),
             Paragraph(f"Statement generated by the {COMPANY} debtor management system on "
                       f"{_day(timezone.localdate())}. Amounts are as recorded in our accounts on that day; "
                       f"payments made since may not yet be reflected.", st["foot"])]
    doc.build(story)
    return buf.getvalue()


def statement_filename(debtor_name):
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in debtor_name).strip() or "debtor"
    return f"Statement of account - {safe}.pdf"
