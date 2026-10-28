"""Raporu PDF'e yazar (ReportLab).

Türkçe karakterler için TrueType yazı tipi gerekir. Lisans nedeniyle Windows yazı tipleri pakete
gömülmez; program klasöründeki fonts/ (ör. DejaVuSans) ya da sistemdeki Arial/Segoe UI kullanılır.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from yapimmaliyet import APP_NAME, __version__
from yapimmaliyet.branding import CORPORATE, letterhead_path
from yapimmaliyet.infra.errors import AppError
from yapimmaliyet.infra.paths import app_base_dir
from yapimmaliyet.reports.model import ColKind, ReportDoc, fmt

FONT, FONT_BOLD = "YMSans", "YMSans-Bold"
LOGO_H = 9 * mm           # antetteki logo yüksekliği
HEAD_TOP = 22 * mm        # antet varken içeriğin üst boşluğu
_CANDIDATES = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("calibri.ttf", "calibrib.ttf"),
]


def top_margin(default: float) -> float:
    """Antet (logo) varsa içerik onun altından başlar."""
    return HEAD_TOP if letterhead_path() else default


def draw_letterhead(canvas, page_w: float, page_h: float, margin: float) -> None:
    """Her sayfanın üstüne antet: solda simge + logo, altında kurumsal renkte ince çizgi. Logo yoksa çizilmez."""
    logo = letterhead_path()
    if logo is None:
        return
    img = ImageReader(str(logo))
    iw, ih = img.getSize()
    top = page_h - 7 * mm
    canvas.saveState()
    canvas.drawImage(img, margin, top - LOGO_H, LOGO_H * iw / ih, LOGO_H, mask="auto")
    canvas.setStrokeColor(colors.HexColor(CORPORATE))
    canvas.setLineWidth(0.6)
    canvas.line(margin, top - LOGO_H - 2 * mm, page_w - margin, top - LOGO_H - 2 * mm)
    canvas.restoreState()


def _font_dirs() -> list[Path]:
    base = app_base_dir()
    return [base / "_internal" / "fonts", base / "fonts", Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]


def register_fonts() -> str:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return FONT
    for regular, bold in _CANDIDATES:
        for d in _font_dirs():
            r, b = d / regular, d / bold
            if r.is_file():
                pdfmetrics.registerFont(TTFont(FONT, str(r)))
                pdfmetrics.registerFont(TTFont(FONT_BOLD, str(b if b.is_file() else r)))
                return FONT
    raise AppError("PDF için Türkçe karakter destekli yazı tipi bulunamadı. Program klasöründeki 'fonts' "
                   "klasörüne DejaVuSans.ttf ekleyin.")


def minimum_widths(table):
    rows = table.rows + ([table.total_row] if table.total_row else [])
    result = []
    for i, col in enumerate(table.columns):
        values = [fmt(row[i], col.kind) for row in rows]
        if col.kind is ColKind.TEXT:
            words = [word for value in values + [col.title] for word in value.split()]
            width = min(140, max((pdfmetrics.stringWidth(w, FONT, 7.5) for w in words), default=25))
        else:
            words = col.title.split()
            width = max((pdfmetrics.stringWidth(v, FONT, 7.5) for v in values + words), default=25)
        result.append(max(26, width + 12))
    return result


def table_widths(table, usable):
    minimum = minimum_widths(table)
    extra = max(0, usable - sum(minimum))
    weights = sum(c.width for c in table.columns)
    return [w + extra * c.width / weights for w, c in zip(minimum, table.columns)]


def write_pdf(doc: ReportDoc, path: Path) -> Path:
    register_fonts()
    pagesize = landscape(A4) if doc.landscape else A4
    minimum = max((sum(minimum_widths(s.table)) for s in doc.sections if s.table), default=0)
    if minimum > pagesize[0] - 24 * mm:
        pagesize = landscape(A3)
    small = ParagraphStyle("small", fontName=FONT, fontSize=7.5, leading=9)
    small_r = ParagraphStyle("small_r", parent=small, alignment=TA_RIGHT)
    head = ParagraphStyle("head", fontName=FONT_BOLD, fontSize=7.5, leading=9)
    body = ParagraphStyle("body", fontName=FONT, fontSize=9, leading=11.5)
    h1 = ParagraphStyle("h1", fontName=FONT_BOLD, fontSize=14, leading=18,
                        textColor=colors.HexColor("#a40000") if doc.draft else colors.black)
    h2 = ParagraphStyle("h2", fontName=FONT_BOLD, fontSize=11, leading=14, spaceBefore=8, spaceAfter=4)

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = [Paragraph(esc(doc.title) + (" — TASLAK (kesinleşmemiş)" if doc.draft else ""), h1)]
    for k, v in doc.meta:
        story.append(Paragraph(f"<b>{esc(k)}:</b> {esc(v)}", body))
    usable = pagesize[0] - 24 * mm
    for s in doc.sections:
        story.append(Paragraph(esc(s.title), h2))
        for p in s.paragraphs:
            story.append(Paragraph(esc(p), body))
        t = s.table
        if t is None:
            continue
        widths = table_widths(t, usable)
        data = [[Paragraph(esc(c.title), head) for c in t.columns]]
        for row in t.rows + ([t.total_row] if t.total_row else []):
            data.append([Paragraph(esc(fmt(v, c.kind)), small if c.kind in (ColKind.TEXT,) else small_r)
                         for v, c in zip(row, t.columns)])
        table = LongTable(data, colWidths=widths, repeatRows=1)
        style = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbbcbc")),
                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ece2e2")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]
        if t.total_row:
            style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f4f0f0")))
        table.setStyle(TableStyle(style))
        story += [table, Spacer(1, 4)]
    if doc.notes:
        story.append(Paragraph("Notlar", h2))
        story += [Paragraph("• " + esc(n), body) for n in doc.notes]

    generated = datetime.now().strftime("%d.%m.%Y %H:%M")

    def footer(canvas, _doc):
        draw_letterhead(canvas, pagesize[0], pagesize[1], 12 * mm)
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.drawString(12 * mm, 7 * mm, f"{APP_NAME} {__version__} · Oluşturma: {generated}"
                          + (" · TASLAK" if doc.draft else ""))
        canvas.drawRightString(pagesize[0] - 12 * mm, 7 * mm, f"Sayfa {_doc.page}")
        canvas.restoreState()

    tmp = path.with_suffix(".pdf.tmp")
    SimpleDocTemplate(str(tmp), pagesize=pagesize, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=top_margin(12 * mm),
                      bottomMargin=14 * mm, title=doc.title, author=APP_NAME).build(
        story, onFirstPage=footer, onLaterPages=footer)
    os.replace(tmp, path)
    return path
