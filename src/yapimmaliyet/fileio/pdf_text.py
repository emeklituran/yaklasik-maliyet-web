"""Metin tabanlı PDF okuyucu (PyMuPDF).

Metin katmanı olmayan (taranmış) sayfalar ayrıca listelenir; bunlar için OCR gerekir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from yapimmaliyet.fileio.pdf_layout import PageWords, Word, pages_to_rows
from yapimmaliyet.fileio.raw_table import RawTable, SourceKind
from yapimmaliyet.infra.errors import ValidationError

MIN_TEXT_CHARS = 20


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    text_pages: list[int]
    scanned_pages: list[int]  # metin yok, görüntü var → OCR gerekli
    empty_pages: list[int]


def open_pdf(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError, OSError) as exc:
        raise ValidationError("PDF dosyası açılamadı; dosya bozuk olabilir.", detail=str(exc)) from exc
    if doc.needs_pass:
        doc.close()
        raise ValidationError("PDF parola korumalı. Parolasız bir kopyasıyla yeniden deneyin.")
    return doc


def analyze_pdf(path: Path) -> PdfInfo:
    doc = open_pdf(path)
    try:
        text_pages, scanned, empty = [], [], []
        for i, page in enumerate(doc, start=1):
            if len(page.get_text().strip()) >= MIN_TEXT_CHARS:
                text_pages.append(i)
            elif page.get_images():
                scanned.append(i)
            else:
                empty.append(i)
        return PdfInfo(doc.page_count, text_pages, scanned, empty)
    finally:
        doc.close()


def read_pdf_text(path: Path, pages: list[int] | None = None) -> RawTable:
    doc = open_pdf(path)
    try:
        selected = pages or list(range(1, doc.page_count + 1))
        page_words = []
        for no in selected:
            page = doc[no - 1]
            # bazı yazı tiplerinde "-" glifi görünmez yumuşak tire (U+00AD) olarak çıkar: görünen tireye çevrilir
            words = [Word(w[0], w[1], w[2], w[3], w[4].replace("­", "-")) for w in page.get_text("words")
                     if w[4].strip()]
            page_words.append(PageWords(no, page.rect.width, words))
    finally:
        doc.close()
    rows = pages_to_rows(page_words)
    if not rows:
        raise ValidationError("PDF'te okunabilir metin bulunamadı. Taranmış bir belgeyse OCR kullanın.")
    table = RawTable(f"{path.name} (metin)", rows, SourceKind.PDF_METIN)
    table.warnings.append("PDF tablo yapısı kelime konumlarından çıkarıldı. Sütunların doğru ayrıldığını "
                          "önizlemede kontrol edin.")
    return table
