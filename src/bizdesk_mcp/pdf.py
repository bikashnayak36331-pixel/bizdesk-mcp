"""Render a quote or invoice to a clean one-page PDF with ReportLab."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#616E7C")
ACCENT = colors.HexColor("#0B6E4F")
LINE = colors.HexColor("#D9E2EC")
SOFT = colors.HexColor("#F0F4F8")

STATUS_LABELS = {
    "paid": ("PAID", ACCENT),
    "partially_paid": ("PART PAID", colors.HexColor("#B7791F")),
    "void": ("VOID", colors.HexColor("#9B2C2C")),
}


def _p(text: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def render_document_pdf(doc: Mapping[str, Any], profile: Mapping[str, Any], path: str | Path) -> Path:
    """Write ``doc`` (from BizDesk.get_document) to ``path`` and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = getSampleStyleSheet()["Normal"]
    s = {
        "body": ParagraphStyle("body", parent=base, fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK),
        "muted": ParagraphStyle("muted", parent=base, fontName="Helvetica", fontSize=8.5, leading=12, textColor=MUTED),
        "label": ParagraphStyle(
            "label", parent=base, fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=MUTED
        ),
        "biz": ParagraphStyle("biz", parent=base, fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK),
        "title": ParagraphStyle(
            "title",
            parent=base,
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=26,
            textColor=ACCENT,
            alignment=TA_RIGHT,
        ),
        "right": ParagraphStyle(
            "right", parent=base, fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK, alignment=TA_RIGHT
        ),
        "rightb": ParagraphStyle(
            "rightb",
            parent=base,
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=14,
            textColor=INK,
            alignment=TA_RIGHT,
        ),
        "th": ParagraphStyle(
            "th", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white
        ),
        "thr": ParagraphStyle(
            "thr",
            parent=base,
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.white,
            alignment=TA_RIGHT,
        ),
    }
    is_invoice = doc["kind"] == "invoice"
    title = "INVOICE" if is_invoice else "QUOTE"
    story: list = []

    # Header: business on the left, document title and number on the right
    biz_lines = [profile.get("address", ""), profile.get("email", ""), profile.get("phone", "")]
    if profile.get("tax_id"):
        biz_lines.append(f"Tax ID: {profile['tax_id']}")
    biz = [_p(profile.get("business_name", ""), s["biz"])] + [_p(x, s["muted"]) for x in biz_lines if x]
    status = STATUS_LABELS.get(doc["status"]) if is_invoice else None
    right = [_p(title, s["title"]), _p(doc["number"], s["right"])]
    if status:
        hexcolor = "#" + status[1].hexval()[2:]
        right.append(Paragraph(f'<font color="{hexcolor}"><b>{status[0]}</b></font>', s["right"]))
    header = Table([[biz, right]], colWidths=[110 * mm, 64 * mm])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story += [header, Spacer(1, 9 * mm)]

    # Bill-to and dates
    client = doc["client"]
    bill = [_p("BILL TO" if is_invoice else "PREPARED FOR", s["label"]), _p(client.get("name"), s["body"])]
    for line in (client.get("company"), client.get("address"), client.get("email")):
        if line:
            bill.append(_p(line, s["muted"]))
    date_rows = [("Issue date", doc["issue_date"])]
    if is_invoice:
        date_rows.append(("Due date", doc.get("due_date")))
        if doc.get("from_quote"):
            date_rows.append(("From quote", doc["from_quote"]))
    else:
        date_rows.append(("Valid until", doc.get("valid_until")))
    dates = Table([[_p(k.upper(), s["label"]), _p(v, s["right"])] for k, v in date_rows], colWidths=[30 * mm, 34 * mm])
    dates.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    meta = Table([[bill, dates]], colWidths=[110 * mm, 64 * mm])
    meta.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story += [meta, Spacer(1, 8 * mm)]

    # Line items
    rows = [[_p("DESCRIPTION", s["th"]), _p("QTY", s["thr"]), _p("UNIT PRICE", s["thr"]), _p("AMOUNT", s["thr"])]]
    for item in doc["items"]:
        rows.append(
            [
                _p(item["description"], s["body"]),
                _p(item["quantity"], s["right"]),
                _p(item["unit_price"], s["right"]),
                _p(item["line_total"], s["right"]),
            ]
        )
    items = Table(rows, colWidths=[94 * mm, 18 * mm, 30 * mm, 32 * mm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
    ]
    for r in range(2, len(rows), 2):
        style.append(("BACKGROUND", (0, r), (-1, r), SOFT))
    items.setStyle(TableStyle(style))
    story += [items, Spacer(1, 5 * mm)]

    # Totals
    tot = [("Subtotal", doc["subtotal"]), (f"Tax ({doc['tax_rate']})", doc["tax"]), ("Total", doc["total"])]
    if is_invoice:
        if doc.get("payments"):
            tot.append(("Paid", f"-{doc['paid']}"))
        tot.append(("Balance due", doc["balance_due"]))
    trows = [[_p(k, s["right"]), _p(v, s["right"])] for k, v in tot[:-1]]
    trows.append([_p(tot[-1][0], s["rightb"]), _p(tot[-1][1], s["rightb"])])
    totals = Table(trows, colWidths=[40 * mm, 32 * mm], hAlign="RIGHT")
    last = len(trows) - 1
    totals.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, last), (-1, last), 1, INK),
                ("BACKGROUND", (0, last), (-1, last), SOFT),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story += [totals, Spacer(1, 8 * mm)]

    # Payments, notes and instructions
    if is_invoice and doc.get("payments"):
        story.append(_p("PAYMENTS RECEIVED", s["label"]))
        for pay in doc["payments"]:
            extra = " · ".join(x for x in (pay.get("method"), pay.get("reference")) if x)
            story.append(_p(f"{pay['paid_on']}: {pay['amount']}" + (f" ({extra})" if extra else ""), s["muted"]))
        story.append(Spacer(1, 5 * mm))
    if doc.get("notes"):
        story += [_p("NOTES", s["label"]), _p(doc["notes"], s["body"]), Spacer(1, 5 * mm)]
    if is_invoice and profile.get("payment_instructions") and doc["status"] != "paid":
        story += [_p("HOW TO PAY", s["label"]), _p(profile["payment_instructions"], s["body"])]

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 12 * mm, f"{profile.get('business_name', '')} · {doc['number']}")
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, "Generated with BizDesk MCP")
        canvas.restoreState()

    pdf = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title=f"{title.title()} {doc['number']}",
        author=str(profile.get("business_name", "")),
    )
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return path
