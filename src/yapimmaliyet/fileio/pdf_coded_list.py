"""Kod satırlı birim fiyat listesi ayrıştırıcısı (ÇŞB birim fiyat kitapları ve benzerleri).

Bu tür listelerde her kalem sol kenardaki poz koduyla başlar; tanım birkaç satıra yayılır;
birim ve fiyat(lar) sağ sütunlardadır. Bölümden bölüme sütun düzeni değişebilir
(rayiç: birim + satın alma yeri + fiyat; inşaat: birim + fiyat; tesisat: montajlı birim fiyat +
montaj bedeli, birim tanım içinde "(Ölçü:Ad)"). Kurallar:
- Kalın satırlar bölüm/kategori başlığıdır; kalemin tanımına eklenmez.
- Koddan sonraki normal satırlar o kalemin tanımının devamıdır (sayfa sonunu aşabilir).
- "Not:" ile başlayan bloklar tanıma eklenmez.
- Fiyatı olmayan kalem "başlık poz"dur: aynı kod önekiyle gelen alt kalemler başlığın ilk satırını
  tanımlarına ön ek, birimini de (kendi birimleri yoksa) birim olarak alır.
Çıktı standart sütunlu ham tablodur; doğrulama ve onay yine önizleme adımındadır.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from yapimmaliyet.fileio.pdf_text import open_pdf
from yapimmaliyet.fileio.raw_table import RawRow, RawTable, SourceKind

CODE_RE = re.compile(r"^[A-ZÇĞİÖŞÜ]{0,4}[.\-]?\d{1,3}(?:[.\-/]\d{1,5}){2,4}[A-Z]?$")
NUMBER_RE = re.compile(r"^-?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$")
UNIT_RE = re.compile(
    r"^(m|mt|metre|m²|m2|m³|m3|dm³|dm3|cm|mm|km|km²|km2|dekar|da|ha|hektar|ster|ad\.?|adet|ton|t|kg|gr|g|"
    r"lt\.?|l|litre|sa|saat|gün|ay|yıl|"
    r"takım|tk|set|kwh|kw|kva|kvar|dk|çift|top|rulo|paket|kutu|sefer|kişi|nokta|hat|daire|m²/ay)$",
    re.IGNORECASE)
# "(Ölçü: Ad.)", "(Ölçü:m2 ):", "Ölçü:Ad" — yalnızca ilk sözcük birimdir; parantez kapanmamış olabilir.
# Birimden sonraki kısa artık yeni bir parantezi yutmaz: "Ölçü: Ad. (TS EN 1036)".
OLCU_RE = re.compile(r"\(?\s*Ölçü\s*:\s*([^\s():;,]+)[^()]{0,6}\)?\s*:?", re.IGNORECASE)
PAGE_NO_RE = re.compile(r"^-\s*\d+\s*-$")
HEADER_WORDS = {"poz", "no", "tanım", "tanımı", "birim", "birimi", "fiyat", "fiyatı", "tl", "(tl)", "bedeli",
                "montaj", "montajlı", "rayiç", "ölçü", "satın", "alma", "yeri", "güncel", "endeksleriyle", "tüik",
                "cinsi", "işin", "yapılacak", "r", "a"}
DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
SHORT_DESC = 50
SECTION_RE = re.compile(r"^\d{2}\.\d{3}\.\s*-")  # sayfa üstündeki bölüm satırı: "25.200.-Isıtma Sistemleri"
COLUMNS = ["Poz No", "Tanım", "Birim", "Birim Fiyat", "Kategori", "Montaj Bedeli", "Satın Alma Yeri"]


@dataclass
class _Span:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class _Item:
    code: str
    page: int
    line: int
    desc: list[str]
    unit: str | None
    numbers: list[str]
    place: str | None
    category: str | None
    has_montaj: bool
    heading: str | None = None  # kalemden önceki son kalın başlık satırı


def _spans(page) -> list[_Span]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                # bölünmez boşluk ve yumuşak tire PDF'lerde sık görülür; karşılaştırmalar için normalleştirilir
                text = s["text"].replace("\xa0", " ").replace("\xad", "-").strip()
                if text and not PAGE_NO_RE.match(text):
                    bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
                    out.append(_Span(*s["bbox"], text, bold))
    return out


def _lines(spans: list[_Span]) -> list[list[_Span]]:
    lines: list[list[_Span]] = []
    for s in sorted(spans, key=lambda s: (s.yc, s.x0)):
        if lines and abs(lines[-1][0].yc - s.yc) <= 3:
            lines[-1].append(s)
        else:
            lines.append([s])
    return [sorted(ln, key=lambda s: s.x0) for ln in lines]


def _text_lines(spans: list[_Span]) -> list[str]:
    """Aynı hücre grubundaki yayılımları görsel satırlara ayırarak metin listesi döndürür."""
    out: list[list[_Span]] = []
    for s in spans:
        if out and abs(out[-1][0].yc - s.yc) <= 3:
            out[-1].append(s)
        else:
            out.append([s])
    return [" ".join(x.text for x in sorted(ln, key=lambda s: s.x0)) for ln in out]


def _row_separators(page) -> list[float]:
    """Tablo satır sınırları: yatay çizgiler ve hücre dikdörtgenlerinin üst/alt kenarları."""
    ys = set()
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l" and abs(item[1].y - item[2].y) < 0.6 and abs(item[1].x - item[2].x) > 20:
                ys.add(round(item[1].y * 2) / 2)
            elif item[0] == "re" and item[1].width > 20:
                ys.update((round(item[1].y0 * 2) / 2, round(item[1].y1 * 2) / 2))
    return sorted(ys)


def _bands(lines: list[list[_Span]], seps: list[float]) -> list[list[_Span]]:
    """Satırları, aralarında tablo çizgisi bulunmayan gruplara (hücre satırlarına) toplar.

    Kod hücrede dikey ortalanmış olsa da tanımın bütün satırları aynı kaleme düşer.
    Yeterli çizgi yoksa her metin satırı ayrı grup kalır.
    """
    if len(seps) < 5:
        return lines
    groups: dict[int, list[_Span]] = {}
    order: list[int] = []
    for ln in lines:
        key = bisect_right(seps, ln[0].yc)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].extend(ln)
    return [sorted(groups[k], key=lambda s: (round(s.yc), s.x0)) for k in order]


def _is_header_line(line: list[_Span]) -> bool:
    words = [w.lower() for s in line for w in s.text.split()]
    return bool(words) and all(w in HEADER_WORDS or DATE_RE.match(w) for w in words)


def _layout(lines: list[list[_Span]], code_x: float, page_width: float) -> tuple[int, float, bool, bool]:
    """(tablo başlığından sonraki ilk satır indeksi, sağ sütun sınırı, montaj sütunu var mı, satın alma yeri var mı).

    Tablo başlığı bulunamazsa ilk değer -1 olur (kapak, açıklama sayfası: atlanır).
    """
    last_header, right_x, montaj, place = -1, None, False, False
    for i, ln in enumerate(lines[:14]):
        text = " ".join(s.text for s in ln).lower()
        if _is_header_line(ln) or ("poz" in text and "no" in text and ln[0].x0 < code_x + 40):
            last_header = i
            for s in ln:
                if s.x0 > page_width * 0.6:
                    right_x = s.x0 if right_x is None else min(right_x, s.x0)
            montaj = montaj or "montaj" in text
            place = place or "satın" in text
    if last_header < 0:
        return -1, page_width * 0.66, False, False
    return last_header + 1, (right_x - 6) if right_x else page_width * 0.66, montaj, place


def _code_x(doc, pages: list[int]) -> float:
    xs = Counter()
    for no in pages[:40]:
        for w in doc[no - 1].get_text("words"):
            if w[0] < doc[no - 1].rect.width * 0.2 and CODE_RE.match(w[4]):
                xs[round(w[0])] += 1
    return float(xs.most_common(1)[0][0]) if xs else 45.0


ANALYSIS_MARKERS = ("analizin adı", "birim fiyat analizi", "malzeme bileşenleri", "montaj bileşenleri",
                    "genel fiyat analizi")


def is_analysis_book(path: Path, pages: list[int]) -> bool:
    """Birim fiyat ANALİZ kitabı mı? (Her sayfada bir pozun girdileri; bileşen satırları poz değildir.)"""
    doc = open_pdf(path)
    try:
        sample = pages[:: max(1, len(pages) // 20)][:20]
        hits = sum(1 for no in sample
                   if any(m in doc[no - 1].get_text().replace("\xa0", " ").lower() for m in ANALYSIS_MARKERS))
        return bool(sample) and hits >= len(sample) / 2
    finally:
        doc.close()


def looks_like_coded_list(path: Path, pages: list[int]) -> bool:
    """Örnek sayfaların yarısında, solda kod ve aynı satırda sayı olan en az üç satır varsa True.

    Analiz kitapları (bileşen satırları da kodla başlar) liste sayılmaz.
    """
    if is_analysis_book(path, pages):
        return False
    doc = open_pdf(path)
    try:
        sample = pages[:: max(1, len(pages) // 30)][:30]
        hits = 0
        for no in sample:
            page = doc[no - 1]
            lines: dict[int, list] = {}
            for w in page.get_text("words"):
                lines.setdefault(round((w[1] + w[3]) / 6), []).append(w)
            n = sum(1 for ws in lines.values()
                    if any(w[0] < page.rect.width * 0.2 and CODE_RE.match(w[4]) for w in ws)
                    and any(w[0] > page.rect.width * 0.6 and NUMBER_RE.match(w[4]) for w in ws))
            hits += n >= 3
        return bool(sample) and hits >= len(sample) / 2
    finally:
        doc.close()


def read_coded_list(path: Path, pages: list[int], progress=None) -> RawTable:
    doc = open_pdf(path)
    items: list[_Item] = []
    notes = 0
    try:
        code_x = _code_x(doc, pages)
        current: _Item | None = None
        section: str | None = None
        category: str | None = None
        heading: str | None = None
        in_heading = in_note = False
        for n, no in enumerate(pages, start=1):
            page = doc[no - 1]
            lines = _lines(_spans(page))
            start, right_x, montaj, place_col = _layout(lines, code_x, page.rect.width)
            if start < 0:  # tablo içermeyen sayfa (bölüm kapağı, genel hükümler)
                continue
            title = next((" ".join(s.text for s in ln) for ln in lines[:start]
                          if SECTION_RE.match(" ".join(s.text for s in ln))), None)
            if title and title != section:  # yeni bölüm: önceki bölümün başlıkları taşınmaz
                section, category, heading, in_heading = title, title, None, False
            bottom = page.rect.height - 30
            body = [ln for ln in lines[start:] if ln[0].y0 <= bottom]
            for line_no, ln in enumerate(_bands(body, _row_separators(page)), start=1):
                code = next((s for s in ln if s.x0 < code_x + 40 and CODE_RE.match(s.text)), None)
                rest = [s for s in ln if s is not code and s.x0 >= code_x + 40]
                right = [s for s in rest if s.x0 >= right_x]
                nums = [s.text for s in right if NUMBER_RE.match(s.text)]
                first_num_x = min((s.x0 for s in right if NUMBER_RE.match(s.text)), default=9e9)
                units = [s for s in right if s.x0 < first_num_x and UNIT_RE.match(s.text.rstrip("."))]
                if not units:   # "1000 adet", "100 m" gibi katsayılı birim: sayı + birim, fiyat en sağda kalır
                    units, nums = _scaled_unit(right, nums)
                others = [s for s in right if s.text not in nums and s not in units]
                place = " ".join(s.text for s in others) if place_col and others else None
                desc_spans = sorted([s for s in rest if s.x0 < right_x] + ([] if place else others),
                                    key=lambda s: (round(s.yc), s.x0))
                text = " ".join(s.text for s in desc_spans).strip()
                if code is not None:
                    in_heading = in_note = False
                    current = _Item(code.text, no, line_no, _text_lines(desc_spans),
                                    units[0].text.rstrip(".") if units else None, nums, place, category, montaj,
                                    heading=heading)
                    items.append(current)
                    continue
                if not text and not nums:
                    continue
                if all(s.bold for s in desc_spans) and desc_spans:
                    head = _text_lines(desc_spans)[0]
                    base = section if section else None
                    if in_heading and category and len(category) + len(head) < 160:
                        category = f"{category} — {head}"
                    elif not in_heading:
                        category = f"{base} — {head}" if base else head
                    heading = head
                    in_heading, in_note, current = True, False, None
                    continue
                in_heading = False
                if text.lower().startswith("not:") or (in_note and re.match(r"^\d+\s*[-.)]", text)):
                    in_note = True
                    notes += 1
                    continue
                if in_note:
                    notes += 1
                    continue
                if current is not None:
                    current.desc.extend(_text_lines(desc_spans))
                    if not current.numbers and nums:
                        current.numbers = nums
                    if not current.unit and units:
                        current.unit = units[0].text.rstrip(".")
            if progress:
                progress(n, len(pages))
    finally:
        doc.close()
    return _to_table(path, items, notes)


def _scaled_unit(right: list[_Span], nums: list[str]) -> tuple[list[_Span], list[str]]:
    """Birim sütunundaki "1000 adet" gibi katsayılı birimi tek birim olarak döndürür; katsayı fiyat sayılmaz."""
    spans = sorted(right, key=lambda s: s.x0)
    for s in spans:   # tek parça: "1000 adet"
        head, _, tail = s.text.partition(" ")
        if tail and NUMBER_RE.match(head) and UNIT_RE.match(tail.rstrip(".")):
            return [s], nums
    for i, s in enumerate(spans[1:-1], start=1):
        prev = spans[i - 1]
        if (UNIT_RE.match(s.text.rstrip(".")) and NUMBER_RE.match(prev.text) and s.x0 - prev.x1 < 8
                and any(NUMBER_RE.match(x.text) for x in spans[i + 1:])):
            merged = _Span(prev.x0, min(prev.y0, s.y0), s.x1, max(prev.y1, s.y1), f"{prev.text} {s.text}", s.bold)
            rest = list(nums)
            rest.remove(prev.text)
            return [merged], rest
    return [], nums


def clean_unit(text: str | None) -> str | None:
    if not text:
        return None
    u = text.strip().rstrip(".:;,").strip()
    return u or None


def _clean_desc(parts: list[str]) -> tuple[str, str | None]:
    """Tanımı birleştirir; içindeki "(Ölçü: X)" ifadesini çıkarıp birim olarak döndürür."""
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    m = OLCU_RE.search(text)
    unit = clean_unit(m.group(1)) if m else None
    if m:
        text = re.sub(r"\s+", " ", text[:m.start()] + " " + text[m.end():]).strip(" :;,")
    return text, unit


def _prefix(title: str, desc: str) -> str:
    title = title.rstrip(" :;,")
    return f"{title}: {desc}" if desc else title


@dataclass
class _Parent:
    code: str
    title: str
    unit: str | None


def _to_table(path: Path, items: list[_Item], notes: int) -> RawTable:
    rows = [RawRow(list(COLUMNS), "tablo başlığı")]
    parents: list[_Parent] = []  # aynı kod ailesindeki iç içe başlık pozlar
    n_parents = orphan = 0
    for it in items:
        desc, olcu = _clean_desc(it.desc)
        family = it.code[:-3]
        parents = [p for p in parents if p.code[:-3] == family and p.code < it.code]
        if not it.numbers:
            first, first_unit = _clean_desc(it.desc[:1])
            parents.append(_Parent(it.code, first or desc, first_unit or olcu or clean_unit(it.unit)))
            n_parents += 1
            continue
        unit = clean_unit(it.unit) or olcu
        heading, head_unit = _clean_desc([it.heading]) if it.heading else (None, None)
        if parents:
            desc = _prefix(parents[-1].title, desc)
            unit = unit or next((p.unit for p in reversed(parents) if p.unit), None)
        elif heading and len(desc) < SHORT_DESC:
            # "DN 80", "(Tip 33) 400" gibi tek başına anlamsız kısa tanımlar başlıkla birlikte verilir
            desc = _prefix(heading, desc)
        category, cat_unit = _clean_desc([it.category]) if it.category else (None, None)
        unit = unit or head_unit or cat_unit
        price = it.numbers[0] if it.has_montaj and len(it.numbers) >= 2 else it.numbers[-1]
        montaj = it.numbers[1] if it.has_montaj and len(it.numbers) >= 2 else None
        if not desc:
            orphan += 1
        rows.append(RawRow([it.code, desc or None, unit, price, category or None, montaj, it.place],
                           f"s.{it.page} sat.{it.line}", page=it.page, line=it.line))
    parents_count = n_parents
    table = RawTable(f"{path.name} (kod satırlı liste)", rows, SourceKind.PDF_METIN)
    no_unit = sum(1 for r in rows[1:] if not r.values[2])
    table.meta = {"Ayrıştırıcı": "Kod satırlı birim fiyat listesi", "Kalem": str(len(rows) - 1),
                  "Başlık poz": str(parents_count)}
    table.warnings.append(
        f"{len(rows) - 1} fiyatlı kalem bulundu. {parents_count} başlık poz (fiyatsız) alt kalemlerin tanımına ön "
        f"ek olarak eklendi; {notes} not satırı tanımlara katılmadı. Sonuçları önizlemede kontrol edin.")
    if no_unit:
        table.warnings.append(f"{no_unit} kalemin birimi kaynakta bulunamadı (ör. birim sütunu olmayan rayiç "
                              "listeleri). Birim uydurulmaz; önizlemede düzeltin ya da toplu doldurun.")
    if orphan:
        table.warnings.append(f"{orphan} kalemin tanımı boş; önizlemede hata olarak görünecek.")
    return table
