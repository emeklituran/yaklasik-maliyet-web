"""Belge dosyasını (Packet) PDF'e yazar (ReportLab): her belge yeni sayfada başlar, dikey/yatay sayfa belgeye göre
seçilir. Uzun tablolar başlık satırı tekrarlanarak sayfalara bölünür; imza bloğu bölünmez (KeepTogether).
Taslakta her sayfaya çapraz "TASLAK" filigranı basılır; alt bilgide kontrol amaçlı olduğu yazar.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from yapimmaliyet import APP_NAME, __version__
from yapimmaliyet.reports.model import ColKind, fmt
from yapimmaliyet.reports.packet import KeyValues, Packet, Page, Signatures, TableBlock, Text
from yapimmaliyet.reports.pdf_writer import FONT, FONT_BOLD, draw_letterhead, register_fonts, top_margin

MARGIN = 11 * mm
GRID = colors.HexColor("#a39494")


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


class _Styles:
    def __init__(self, draft: bool):
        self.small = ParagraphStyle("s", fontName=FONT, fontSize=7, leading=8.4)
        self.small_r = ParagraphStyle("sr", parent=self.small, alignment=TA_RIGHT)
        self.small_b = ParagraphStyle("sb", parent=self.small, fontName=FONT_BOLD)
        self.small_br = ParagraphStyle("sbr", parent=self.small_b, alignment=TA_RIGHT)
        self.head = ParagraphStyle("h", fontName=FONT_BOLD, fontSize=7, leading=8.4, alignment=TA_CENTER)
        self.body = ParagraphStyle("b", fontName=FONT, fontSize=9, leading=11.5)
        self.body_b = ParagraphStyle("bb", parent=self.body, fontName=FONT_BOLD)
        self.note = ParagraphStyle("n", parent=self.body, fontSize=8, leading=10, textColor=colors.HexColor("#444"))
        self.warn = ParagraphStyle("w", parent=self.body, fontSize=8.5, textColor=colors.HexColor("#a40000"))
        self.sub = ParagraphStyle("st", fontName=FONT_BOLD, fontSize=10, leading=13, alignment=TA_CENTER)
        self.title = ParagraphStyle("t", fontName=FONT_BOLD, fontSize=14, leading=18, alignment=TA_CENTER,
                                    textColor=colors.HexColor("#a40000") if draft else colors.black, spaceAfter=4)
        self.h2 = ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=10.5, leading=13, spaceBefore=6, spaceAfter=3)
        self.sig = ParagraphStyle("sig", fontName=FONT, fontSize=8, leading=10, alignment=TA_CENTER)
        self.sig_b = ParagraphStyle("sigb", parent=self.sig, fontName=FONT_BOLD)


def _table(block: TableBlock, width: float, st: _Styles) -> list:
    t = block.table
    total_w = sum(c.width for c in t.columns)
    widths = [width * c.width / total_w for c in t.columns]
    body = t.rows + ([t.total_row] if t.total_row else [])
    data = [[Paragraph(_esc(c.title), st.head) for c in t.columns]]
    bold_rows = set(block.bold_rows) | ({len(body) - 1} if t.total_row else set())
    for i, row in enumerate(body):
        b = i in bold_rows
        data.append([Paragraph(_esc(fmt(v, c.kind)), (st.small_b if b else st.small) if c.kind is ColKind.TEXT
                               else (st.small_br if b else st.small_r)) for v, c in zip(row, t.columns)])
    tbl = LongTable(data, colWidths=widths, repeatRows=1)
    style = [("GRID", (0, 0), (-1, -1), 0.3, GRID), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ece2e2")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.2),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2), ("LEFTPADDING", (0, 0), (-1, -1), 2),
             ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    style += [("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#f4f0f0")) for i in sorted(bold_rows)]
    tbl.setStyle(TableStyle(style))
    out = [Paragraph(_esc(block.title), st.h2)] if block.title else []
    return out + [tbl, Spacer(1, 5)]


def _keyvalues(block: KeyValues, width: float, st: _Styles) -> list:
    data = [[Paragraph(_esc(k), st.body_b), Paragraph(":", st.body), Paragraph(_esc(v), st.body)]
            for k, v in block.rows]
    tbl = Table(data, colWidths=[width * 0.36, 5 * mm, width * 0.64 - 5 * mm])
    tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                             ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
    return [tbl, Spacer(1, 6)]


def _signatures(block: Signatures, width: float, st: _Styles) -> list:
    per_row = 4 if width < 200 * mm else 6
    rows = []
    items = block.items
    for i in range(0, len(items), per_row):
        chunk = items[i:i + per_row]
        rows.append([[Paragraph(_esc(s.role), st.sig_b), Spacer(1, 14 * mm), Paragraph(_esc(s.name), st.sig),
                      Paragraph(_esc(s.title), st.sig)] for s in chunk] + [""] * (per_row - len(chunk)))
    tbl = Table(rows, colWidths=[width / per_row] * per_row)
    tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    parts = [Spacer(1, 8), tbl]
    if block.date_text:
        parts.append(Paragraph(_esc(block.date_text), st.note))
    return [KeepTogether(parts)]


def _header(page: Page, packet: Packet, width: float, st: _Styles) -> list:
    out = [Paragraph(_esc(packet.subtitle), st.sub),
           Paragraph(_esc(page.title) + (" — TASLAK" if packet.draft else ""), st.title)]
    if page.header:
        data = [[Paragraph(_esc(k), st.small_b), Paragraph(_esc(v), st.small_r)] for k, v in page.header]
        tbl = Table(data, colWidths=[32 * mm, 38 * mm], hAlign="RIGHT")
        tbl.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
        out.append(tbl)
    return out + [Spacer(1, 4)]


def write_packet_pdf(packet: Packet, path: Path) -> Path:
    register_fonts()
    st = _Styles(packet.draft)
    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    sizes = {"P": A4, "L": landscape(A4)}

    def decorate(canvas, doc):
        w, h = canvas._pagesize     # şablona göre dikey/yatay; doc.pagesize ilk sayfanınkidir
        draw_letterhead(canvas, w, h, MARGIN)
        canvas.saveState()
        if packet.draft:
            canvas.setFont(FONT_BOLD, 70)
            canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.12))
            canvas.translate(w / 2, h / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, "TASLAK")
            canvas.rotate(-35)
            canvas.translate(-w / 2, -h / 2)
        canvas.setFillColor(colors.black)
        canvas.setFont(FONT, 6.5)
        page_text = f"Sayfa {doc.page}"
        room = w - 2 * MARGIN - canvas.stringWidth(page_text, FONT, 6.5) - 6 * mm
        text = f"{APP_NAME} {__version__} · {generated} · {packet.footer_note}"
        while canvas.stringWidth(text, FONT, 6.5) > room and len(text) > 20:
            text = text[:-2].rstrip() + "…" if not text.endswith("…") else text[:-3].rstrip() + "…"
        canvas.drawString(MARGIN, 6 * mm, text)
        canvas.drawRightString(w - MARGIN, 6 * mm, page_text)
        canvas.restoreState()

    head = top_margin(MARGIN)
    templates = [PageTemplate(id=k, frames=[Frame(MARGIN, 11 * mm, s[0] - 2 * MARGIN, s[1] - head - 11 * mm,
                                                  id=f"f{k}")], pagesize=s, onPage=decorate)
                 for k, s in sizes.items()]
    first = "L" if packet.pages and packet.pages[0].landscape else "P"
    templates.sort(key=lambda t: t.id != first)
    story: list = []
    for i, page in enumerate(packet.pages):
        key = "L" if page.landscape else "P"
        width = sizes[key][0] - 2 * MARGIN
        if i:
            story += [NextPageTemplate(key), PageBreak()]
        story += _header(page, packet, width, st)
        for block in page.blocks:
            if isinstance(block, Text):
                style = {"heading": st.h2, "note": st.note, "warning": st.warn}.get(block.style, st.body)
                story.append(Paragraph(_esc(block.text), style))
            elif isinstance(block, KeyValues):
                story += _keyvalues(block, width, st)
            elif isinstance(block, TableBlock):
                story += _table(block, width, st)
            elif isinstance(block, Signatures):
                story += _signatures(block, width, st)
    tmp = path.with_suffix(".pdf.tmp")
    doc = BaseDocTemplate(str(tmp), pagesize=sizes[first], leftMargin=MARGIN, rightMargin=MARGIN, topMargin=head,
                          bottomMargin=11 * mm, title=packet.title, author=APP_NAME)
    doc.addPageTemplates(templates)
    doc.build(story)
    os.replace(tmp, path)
    return path
