"""Koordinatlı kelimelerden satır ve sütun oluşturma (metin PDF ve OCR ortak).

Yöntem:
1. Kelimeler dikey merkezlerine göre satırlara gruplanır.
2. Satır içinde büyük yatay boşluklar hücre sınırı kabul edilir.
3. Belge genelinde, en az iki hücreli satırlardaki hücre aralıkları birleştirilerek
   sütun bantları çıkarılır (sayfa genişliğinin büyük kısmını kaplayan başlıklar hariç).
4. Her hücre en çok örtüştüğü banda yerleştirilir.
Sonuç kesin değildir; önizleme ve sütun eşleme adımında kullanıcı tarafından kontrol edilir.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from yapimmaliyet.fileio.raw_table import RawRow


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float | None = None


@dataclass(frozen=True)
class Cell:
    x0: float
    x1: float
    text: str
    conf: float | None


@dataclass(frozen=True)
class PageWords:
    page_no: int  # 1'den başlar
    width: float
    words: list[Word]


def group_lines(words: list[Word]) -> list[list[Word]]:
    if not words:
        return []
    heights = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    tol = (statistics.median(heights) if heights else 10.0) * 0.5
    ordered = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: list[list[Word]] = []
    centre = None
    for w in ordered:
        c = (w.y0 + w.y1) / 2
        if centre is None or abs(c - centre) > tol:
            lines.append([w])
            centre = c
        else:
            lines[-1].append(w)
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def split_cells(line: list[Word], gap: float) -> list[Cell]:
    cells: list[Cell] = []
    group = [line[0]]
    for w in line[1:]:
        if w.x0 - group[-1].x1 > gap:
            cells.append(_cell(group))
            group = [w]
        else:
            group.append(w)
    cells.append(_cell(group))
    return cells


def _cell(words: list[Word]) -> Cell:
    confs = [w.conf for w in words if w.conf is not None]
    return Cell(words[0].x0, max(w.x1 for w in words), " ".join(w.text for w in words),
                min(confs) if confs else None)


def column_bands(lines: list[list[Cell]], page_width: float) -> list[tuple[float, float]]:
    spans = sorted((c.x0, c.x1) for cells in lines if len(cells) >= 2 for c in cells
                   if c.x1 - c.x0 < page_width * 0.6)
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 <= bands[-1][1] + 1.0:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return [(a, b) for a, b in bands]


def _band_index(cell: Cell, bands: list[tuple[float, float]]) -> int:
    best, best_overlap = 0, -1.0
    for i, (a, b) in enumerate(bands):
        overlap = min(cell.x1, b) - max(cell.x0, a)
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    if best_overlap <= 0:  # hiçbir bantla örtüşmüyor: merkezine en yakın bant
        mid = (cell.x0 + cell.x1) / 2
        best = min(range(len(bands)), key=lambda i: abs((bands[i][0] + bands[i][1]) / 2 - mid))
    return best


def pages_to_rows(pages: list[PageWords]) -> list[RawRow]:
    all_words = [w for p in pages for w in p.words]
    if not all_words:
        return []
    heights = [w.y1 - w.y0 for w in all_words if w.y1 > w.y0]
    gap = max(5.0, (statistics.median(heights) if heights else 10.0) * 0.8)
    line_h = statistics.median(heights) if heights else 10.0
    page_lines = [(p, [(split_cells(line, gap), (min(w.y0 for w in line), max(w.y1 for w in line)))
                       for line in group_lines(p.words)]) for p in pages]
    width = max(p.width for p in pages)
    bands = column_bands([cells for _, lines in page_lines for cells, _ in lines], width)
    if not bands:
        bands = [(0.0, width)]
    rows: list[RawRow] = []
    for page, lines in page_lines:
        prev: _Placed | None = None
        for line_no, (cells, (top, bottom)) in enumerate(lines, start=1):
            texts: list[str | None] = [None] * len(bands)
            confs: list[float | None] = [None] * len(bands)
            left: dict[int, float] = {}
            for c in cells:
                i = _band_index(c, bands)
                texts[i] = c.text if texts[i] is None else f"{texts[i]} {c.text}"
                left.setdefault(i, c.x0)
                if c.conf is not None:
                    confs[i] = c.conf if confs[i] is None else min(confs[i], c.conf)
            if prev is not None and _is_continuation(prev, texts, left, top, line_h):
                _append(prev.row, texts, confs)
                prev.bottom = bottom
                continue
            has_conf = any(c is not None for c in confs)
            row = RawRow(list(texts), f"s.{page.page_no} sat.{line_no}", page=page.page_no,
                         line=line_no, confidences=confs if has_conf else None)
            rows.append(row)
            prev = _Placed(row, left, bottom)
    return rows


@dataclass
class _Placed:
    row: RawRow
    left: dict[int, float]   # sütun → hücrenin sol kenarı
    bottom: float


def _is_continuation(prev: _Placed, texts: list[str | None], left: dict[int, float], top: float,
                     line_h: float) -> bool:
    """Tanımın alt satıra taşan devamı: tek hücreli, önceki satırın çok hücreli bir metin hücresiyle aynı
    sütunda ve aynı sol kenarda, satır aralığı normal. Başlık/not satırları bu koşulları sağlamaz."""
    filled = [i for i, t in enumerate(texts) if t is not None]
    if len(filled) != 1 or sum(t is not None for t in prev.row.values) < 2:
        return False
    k = filled[0]
    above = prev.row.values[k]
    if above is None or k not in prev.left or abs(left[k] - prev.left[k]) > 3.0:
        return False
    if top - prev.bottom > line_h * 0.5:   # satır içi aralık dardır; kalemler arası boşluk geniştir
        return False
    return not _numeric_like(above) and not _numeric_like(texts[k])


def _numeric_like(text: str | None) -> bool:
    return bool(text) and all(ch.isdigit() or ch in ".,-%" for ch in text.replace(" ", ""))


def _append(row: RawRow, texts: list[str | None], confs: list[float | None]) -> None:
    for i, t in enumerate(texts):
        if t is None:
            continue
        row.values[i] = f"{row.values[i]} {t}"
        if confs[i] is not None and row.confidences is not None and row.confidences[i] is not None:
            row.confidences[i] = min(row.confidences[i], confs[i])
