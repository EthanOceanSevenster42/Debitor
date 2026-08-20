"""Render the lawyer progress report as a detailed, KPI-focused PDF.

The report reads worst-first. Matters are banded by how long they have gone
untouched — Critical / Warning / On track — and each band states its own count
and the money sitting in it, so a fifty-row list still answers "what needs
attention and what is it worth?" at a glance. Every row says what the attorneys
last actually did, what the company still owes, and how far through the workflow
the matter is; every company name links back to its matter page on the site.

Uses reportlab (pure-Python, no system dependencies). ASCII-only punctuation so
the standard Helvetica font renders everything cleanly.
"""
import io
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (Flowable, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

BRAND = colors.HexColor("#0E7C7B")
BRAND_SOFT = colors.HexColor("#f2faf9")
BRAND_LINE = colors.HexColor("#d6efee")
NAVY = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
FAINT = colors.HexColor("#9ca3af")
GRIDC = colors.HexColor("#e5e7eb")
STRIPE = colors.HexColor("#fafafa")
TRACKC = colors.HexColor("#e8eaed")

# Severity bands by days since the matter was last worked.
SEV = {
    "ok":       {"rule": colors.HexColor("#15803d"), "bg": colors.HexColor("#ecfdf5"),
                 "hex": "#15803d", "label": "On track"},
    "warning":  {"rule": colors.HexColor("#b45309"), "bg": colors.HexColor("#fef3c7"),
                 "hex": "#b45309", "label": "Warning"},
    "critical": {"rule": colors.HexColor("#b91c1c"), "bg": colors.HexColor("#fde8e8"),
                 "hex": "#b91c1c", "label": "Critical"},
}

# Workflow step labels run long ("After 28 days - proceed with default listing
# with credit bureaus..."), and one wrapped paragraph would set the height of
# every row on the page. Clipped here, in full on the matter page.
ACTION_CHARS = 52


def _money(v):
    try:
        return "R {:,.2f}".format(float(v))
    except (TypeError, ValueError):
        return "R 0.00"


def _d(dt, fmt="%d %b %Y"):
    """Format a timestamp in the site's timezone; '-' when there isn't one."""
    if not dt:
        return "-"
    try:
        dt = timezone.localtime(dt)
    except (ValueError, TypeError, AttributeError):
        pass
    return dt.strftime(fmt)


def _clip(s, n=ACTION_CHARS):
    """Trim to length on a word boundary, so a clipped label never ends mid-word."""
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;:-") + "..."


def _days(n):
    return "%s day%s" % (n, "" if n == 1 else "s")


class Bar(Flowable):
    """A slim progress bar - reportlab has no primitive for one."""

    def __init__(self, width, pct, height=3.6):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        self.pct = max(0, min(100, pct or 0))

    def wrap(self, *_args):
        return self.width, self.height

    def draw(self):
        c, r = self.canv, self.height / 2.0
        c.setFillColor(TRACKC)
        c.roundRect(0, 0, self.width, self.height, r, stroke=0, fill=1)
        if self.pct:
            # Never thinner than the cap radius, so 1% still reads as a mark.
            w = max(self.height, self.width * self.pct / 100.0)
            c.setFillColor(BRAND)
            c.roundRect(0, 0, w, self.height, r, stroke=0, fill=1)


class _NumberedCanvas(pdfcanvas.Canvas):
    """Two-pass canvas so the footer can say 'Page 2 of 4' - the total isn't
    known until the whole story has been laid out."""

    def __init__(self, *args, **kwargs):
        pdfcanvas.Canvas.__init__(self, *args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            pdfcanvas.Canvas.showPage(self)
        pdfcanvas.Canvas.save(self)

    def _footer(self, total):
        self.saveState()
        self.setStrokeColor(GRIDC)
        self.setLineWidth(0.5)
        self.line(14 * mm, 12 * mm, A4[0] - 14 * mm, 12 * mm)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(FAINT)
        self.drawString(14 * mm, 8 * mm, "FSA Debtor System - Lawyer Progress Report")
        self.drawRightString(A4[0] - 14 * mm, 8 * mm,
                             "Page %d of %d" % (self._pageNumber, total))
        self.restoreState()


def build_report_pdf(ctx):
    """Return PDF bytes for the report context produced by reports.build_lawyer_report."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title="Lawyer Progress Report",
        topMargin=14 * mm, bottomMargin=18 * mm, leftMargin=14 * mm, rightMargin=14 * mm)

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], textColor=BRAND, fontSize=19,
                        leading=22, spaceAfter=1)
    sub = ParagraphStyle("sub", parent=base["Normal"], textColor=MUTED, fontSize=9, leading=12)
    stamp = ParagraphStyle("stamp", parent=sub, alignment=TA_RIGHT)
    h2 = ParagraphStyle("h2", parent=base["Heading2"], textColor=NAVY, fontSize=11.5,
                        spaceBefore=14, spaceAfter=5)
    cell = ParagraphStyle("cell", parent=base["Normal"], fontSize=8.5, leading=10.5)
    cellr = ParagraphStyle("cellr", parent=cell, alignment=TA_RIGHT)
    cellh = ParagraphStyle("cellh", parent=cell, fontName="Helvetica-Bold",
                           textColor=colors.white, fontSize=8)
    cellhr = ParagraphStyle("cellhr", parent=cellh, alignment=TA_RIGHT)
    small = ParagraphStyle("small", parent=base["Normal"], fontSize=8, textColor=MUTED, leading=11)
    kpi_num = ParagraphStyle("kpinum", parent=base["Normal"], fontSize=17, leading=19,
                             alignment=TA_CENTER, textColor=BRAND, fontName="Helvetica-Bold")
    kpi_money = ParagraphStyle("kpimoney", parent=kpi_num, fontSize=11.5, leading=15)
    kpi_lbl = ParagraphStyle("kpilbl", parent=base["Normal"], fontSize=6.8, leading=9,
                             alignment=TA_CENTER, textColor=MUTED)

    k = ctx["kpis"]
    gen, ps = ctx["generated_at"], ctx["period_start"]
    story = []

    # ---- Masthead ----
    head = Table([[
        [Paragraph("Lawyer Progress Report", h1),
         Paragraph("Reporting period: %s to %s" % (_d(ps), _d(gen)), sub)],
        Paragraph("Generated %s<br/>%s active matter(s) with the lawyers"
                  % (_d(gen, "%d %b %Y %H:%M"), k["active"]), stamp),
    ]], colWidths=[doc.width * 0.6, doc.width * 0.4])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, BRAND),
    ]))
    story.append(head)
    story.append(Spacer(1, 10))

    # ---- KPI tiles (two rows of four; money reads smaller so it never clips) ----
    tiles = [
        (k["active"], "Active matters", False),
        (_money(k["owed_active"]), "Owed by these companies", True),
        (k["new"], "New this period", False),
        ("%s%%" % k["avg_completion"], "Avg workflow completion", False),
        (k["idle_7"], "Idle 7+ days", False),
        (k["idle_14"], "Idle 14+ days", False),
        (_money(k["recovered_period"]), "Recovered (period)", True),
        (_money(k["recovered_total"]), "Recovered (all time)", True),
    ]
    tile_rows = [[[Paragraph(str(n), kpi_money if money else kpi_num), Paragraph(lbl, kpi_lbl)]
                  for n, lbl, money in tiles[i:i + 4]] for i in range(0, len(tiles), 4)]
    kt = Table(tile_rows, colWidths=[doc.width / 4.0] * 4)
    kt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_SOFT),
        ("BOX", (0, 0), (-1, -1), 0.5, BRAND_LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(kt)

    # ---- The one line worth reading if nothing else is ----
    story.append(Spacer(1, 8))
    if k["idle_14"]:
        alert = Table([[Paragraph(
            '<font color="%s"><b>%s of the %s active matter(s) have had no activity for 14+ days'
            '</b>, covering %s of debt. %s in litigation, %s still in collections.</font>' % (
                SEV["critical"]["hex"], k["idle_14"], k["active"],
                _money(k["owed_idle_14"]), k["in_litigation"], k["in_collections"]), small)]],
            colWidths=[doc.width])
    else:
        alert = Table([[Paragraph(
            '<font color="%s"><b>Every active matter has been worked in the last fortnight.</b></font> '
            '%s in litigation, %s still in collections.' % (
                SEV["ok"]["hex"], k["in_litigation"], k["in_collections"]), small)]],
            colWidths=[doc.width])
    alert.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1),
         SEV["critical"]["bg"] if k["idle_14"] else SEV["ok"]["bg"]),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5,
         SEV["critical"]["rule"] if k["idle_14"] else SEV["ok"]["rule"]),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(alert)
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "Average %s since a matter was last worked. %s matter(s) closed this period."
        % (_days(k["avg_days_idle"]), k["closed_period"]), small))

    def _table_style(extra=None):
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("GRID", (0, 0), (-1, -1), 0.4, GRIDC),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ] + (extra or []))

    def _link(m):
        return '<a href="%s" color="#0E7C7B"><b>%s</b></a>' % (m["url"], escape(m["name"]))

    def _company_cell(m):
        return Paragraph('%s<br/><font size=7 color="#9ca3af">%s</font>'
                         % (_link(m), escape(m["stage_label"])), cell)

    # ---- Newly handed over ----
    story.append(Paragraph("Newly handed over (%s)" % k["new"], h2))
    if ctx["new_matters"]:
        widths = (0.40, 0.18, 0.21, 0.21)
        data = [[Paragraph("Company", cellh), Paragraph("Owes", cellhr),
                 Paragraph("Handed over", cellh), Paragraph("By", cellh)]]
        for m in ctx["new_matters"]:
            data.append([
                _company_cell(m),
                Paragraph(_money(m["amount_owed"]), cellr),
                Paragraph(_d(m["sent_at"]), cell),
                Paragraph(escape(m["sent_by"] or "-"), cell),
            ])
        t = Table(data, colWidths=[doc.width * c for c in widths], repeatRows=1)
        t.setStyle(_table_style([("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE])]))
        story.append(t)
    else:
        story.append(Paragraph("No companies handed over this period.", small))

    # ---- Active matters, banded by how long they have been idle ----
    story.append(Paragraph("Active matters (%s)" % k["active"], h2))
    if not ctx["active_matters"]:
        story.append(Paragraph("No active matters.", small))
    else:
        widths = (0.24, 0.15, 0.13, 0.31, 0.17)
        barw = doc.width * widths[2] - 12          # cell width less its padding
        headers = [Paragraph("Company / stage", cellh), Paragraph("Owes", cellhr),
                   Paragraph("Progress", cellh), Paragraph("Last action by the lawyers", cellh),
                   Paragraph("Last worked", cellh)]

        for g in ctx["severity_groups"]:
            sev = SEV[g["key"]]
            # The band heading is row 0 of its own table, spanning every column, so
            # it can never be stranded at the foot of a page and it repeats above
            # the column headers wherever a long band continues overleaf.
            band = Paragraph(
                '<font color="%s"><b>%s</b></font> &nbsp;&#183;&nbsp; %s matter%s '
                '&nbsp;&#183;&nbsp; %s owed' % (
                    sev["hex"], g["label"], g["count"], "" if g["count"] == 1 else "s",
                    _money(g["owed"])), small)

            data = [[band, "", "", "", ""], list(headers)]
            for m in g["matters"]:
                la = m["last_action"]
                if la:
                    who = escape(la["who"] or "-")
                    where = ("%s &#183; " % escape(la["section"])) if la.get("section") else ""
                    action = ('%s %s<br/><font size=7 color="#9ca3af">%s%s &#183; %s</font>' % (
                        "&#10003;" if la["kind"] == "step" else "&#9873;",
                        escape(_clip(la["title"])), where, who, _d(la["date"])))
                else:
                    action = '<font color="#9ca3af">Nothing recorded yet</font>'
                data.append([
                    _company_cell(m),
                    Paragraph(_money(m["amount_owed"]), cellr),
                    [Paragraph('<b>%s</b> / %s &nbsp;&nbsp;<font color="#6b7280">%s%%</font>'
                               % (m["done"], m["total"], m["pct"]), cell),
                     Spacer(1, 2.5), Bar(barw, m["pct"])],
                    Paragraph(action, cell),
                    Paragraph('<font color="%s"><b>%s</b></font>'
                              '<br/><font size=7 color="#9ca3af">%s</font>'
                              % (sev["hex"], _days(m["days_idle"]) + " ago",
                                 _d(m["last_worked"])), cell),
                ])
            t = Table(data, colWidths=[doc.width * c for c in widths], repeatRows=2)
            t.setStyle(_table_style([
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), sev["bg"]),
                ("BACKGROUND", (0, 1), (-1, 1), BRAND),
                ("LEFTPADDING", (0, 0), (0, 0), 9),
                ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, STRIPE]),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, sev["rule"]),
            ]))
            story.append(Spacer(1, 9))
            story.append(t)

    # ---- Footer ----
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        'Tick off steps and add comments on the Lawyers page: '
        '<a href="%s" color="#0E7C7B">%s</a>' % (ctx["legal_url"], ctx["legal_url"]), small))
    story.append(Paragraph(
        "Generated automatically by the FSA Debtor System on %s. Company names link "
        "through to their matter." % _d(gen, "%d %b %Y %H:%M"), small))

    doc.build(story, canvasmaker=_NumberedCanvas)
    return buf.getvalue()
