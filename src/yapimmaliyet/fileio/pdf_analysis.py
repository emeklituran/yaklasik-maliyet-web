"""Bakanlık birim fiyat ANALİZ kitapları ayrıştırıcısı (konum tabanlı, PyMuPDF yayılımları).

Analizler (bir pozun bileşenleri ve fiyat oluşumu) ayrı veri yapısına çıkarılır; poz/fiyat tablolarına
yazılmaz. Düzenler: GENEL (inşaat "Genel Fiyat Analizi" / "Yardımcı Analizler": tek tutar sütunu, alt
başlıklar, "Malzeme + İşçilik Tutarı", "25 % Yüklenici kârı...", "1 m² Fiyatı", ardından tarif/ölçü/not;
yardımcı analizlerde kâr satırı yoktur, birim fiyat = bileşen toplamı) ve MALZEME_MONTAJ (mekanik ve
elektrik: "Malzeme/Montaj Bileşenleri" grupları, iki tutar sütunu; özet "Kârsız Toplam" (iki değer),
"Kârsız Malzeme ve Montaj", "Montaj Bedeli", "Birim Fiyat"). Özet değerleri metin sırasına değil konuma
göre (aynı yükseklikteki soldaki en yakın etiket) eşlenir. Bir sayfada birden çok analiz olabilir; analiz
sayfa sonunu aşabilir. Her analiz aritmetik olarak denetlenir; uyuşmazlık düzeltilmez, `warnings`'e
yazılır. Sayılar Decimal'dir (float kullanılmaz).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from yapimmaliyet.fileio.number_format import NumberFormat, parse_number
from yapimmaliyet.fileio.pdf_coded_list import CODE_RE, NUMBER_RE
from yapimmaliyet.fileio.pdf_text import open_pdf
from yapimmaliyet.infra.errors import AppError

TOL = Decimal("0.01")
LAYOUT_GENERAL = "GENEL"
LAYOUT_MM = "MALZEME_MONTAJ"
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
PAGE_NO_RE = re.compile(r"^-\s*\d+\s*-$")
_N = r"-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?"
FRACTION_RE = re.compile(rf"^({_N})/({_N})$")        # "0,00728/4,50": miktar kesir olarak yazılmış
SCI_RE = re.compile(r"^(\d+(?:,\d+)?)e([+-]?\d+)$")  # "8,6e-005": kaynakta bilimsel gösterim
TOTAL_RE = re.compile(r"\bToplamı\b")                 # ara toplam: "Bölüm Toplamı Malzeme" vb.
SUPERSCRIPT = {"2": "²", "3": "³"}
CODE_PREFIX_RE = re.compile(r"^([A-ZÇĞİÖŞÜ]{0,4}[.\-]?\d{1,3}(?:[.\-/]\d{1,5}){2,4})(?:\s+(.+)|([^\d\s.,/\-].*))$")
LINE_TOL = 3.0      # aynı görsel satır sayılan dikey fark (pt)
WRAP_GAP = 12.2     # bileşen tanımının alt satırı bu aralıktan yakındır; alt başlıklar daha uzaktır
CODE_MAX_X = 75.0   # kod sütunu sol kenarı (inşaat ≈39, tesisat ≈32; tanım ≥87)
COMP_HEADER_WORDS = {"poz", "no", "tanımı", "ölçü", "birimi", "miktarı", "birim", "fiyatı", "tutarı", "(tl)",
                     "malzeme", "montaj", "tl"}
NOTE_BREAK_RE = re.compile(r"^(Ölçü\s*:|Not\s*:|Birim Fiyata Dahil|\d+\s*[).-])", re.IGNORECASE)
_LABELS: list[tuple[str, re.Pattern]] = [  # özet etiketleri; ilk eşleşen kullanılır
    ("subtotal", re.compile(r"^(Malzeme\s*\+\s*İşçilik\s+Tutarı|Kârsız\s+Malzeme\s+ve\s+Montaj)", re.IGNORECASE)),
    ("overhead", re.compile(r"^(\d+(?:,\d+)?)\s*%\s*Yüklenici", re.IGNORECASE)),
    ("rate", re.compile(r"^%\s*(\d+(?:,\d+)?)\s*Yüklenici", re.IGNORECASE)),
    ("net_split", re.compile(r"^Kârsız\s+Toplam", re.IGNORECASE)),
    ("montage", re.compile(r"^Montaj\s+Bedeli", re.IGNORECASE)),
    ("unit_price", re.compile(r"^Birim\s+Fiyat\s*\(TL\)", re.IGNORECASE)),
    ("ignore", re.compile(r"^Genel\s+Giderler\s+Dahil", re.IGNORECASE)),
]
_PRICE_LABEL = re.compile(r"Fiyatı\s*:?\s*$", re.IGNORECASE)  # GENEL düzen: "1 m² Fiyatı" (kalın)


class AnalysisParseCancelled(AppError):
    """Ayrıştırma kullanıcı tarafından iptal edildi."""


@dataclass
class ParsedItem:
    line_no: int
    section: str | None
    code: str | None
    description: str
    unit: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None
    quantity_text: str | None = None  # kaynaktaki miktar yazımı ("21/4,50" gibi kesirler korunur)
    column: str | None = None         # MALZEME_MONTAJ: tutarın bulunduğu sütun ("Malzeme" / "Montaj")
    page: int | None = None


@dataclass
class ParsedAnalysis:
    code: str | None
    name: str
    unit: str | None
    date: str | None                         # ISO (YYYY-AA-GG)
    items: list[ParsedItem] = field(default_factory=list)
    subtotal: Decimal | None = None          # kârsız toplam
    overhead_rate: Decimal | None = None     # ör. 0.25
    overhead_amount: Decimal | None = None   # yalnızca kaynakta yazılıysa (GENEL düzen)
    unit_price: Decimal | None = None
    montage_price: Decimal | None = None
    notes: str | None = None
    page: int = 0
    warnings: list[str] = field(default_factory=list)
    layout: str = LAYOUT_GENERAL
    material_total: Decimal | None = None    # MALZEME_MONTAJ: kârsız malzeme toplamı
    labour_total: Decimal | None = None      # MALZEME_MONTAJ: kârsız montaj toplamı
    end_page: int = 0

    @property
    def check_ok(self) -> bool:
        return not self.warnings

    @property
    def group_label(self) -> str | None:
        return LAYOUT_MM if self.layout == LAYOUT_MM else None


@dataclass
class _Span:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool
    size: float = 10.0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


def to_decimal_tr(text: str) -> Decimal:
    """Kaynaktaki sayı ("1.510,89", "-0,05", "205", "21/4,50", "8,6e-005") → Decimal."""
    s = text.strip()
    if m := FRACTION_RE.match(s):
        num, den = (parse_number(g, NumberFormat.TR) for g in m.groups())
        if den == 0:
            raise ValueError(f"Sıfıra bölünen miktar: {text}")
        return num / den
    if m := SCI_RE.match(s):
        try:
            return Decimal(f"{m.group(1).replace(',', '.')}E{m.group(2)}")
        except InvalidOperation as exc:
            raise ValueError(f"Geçersiz sayı: {text}") from exc
    return parse_number(s, NumberFormat.TR)


def _iso(text: str) -> str | None:
    d, mo, y = (int(g) for g in m.groups()) if (m := DATE_RE.match(text)) else (0, 0, 0)
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= d <= 31 and 1 <= mo <= 12 else None


def _page_spans(page) -> list[_Span]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if not (text := re.sub(r"\s+", " ", s["text"].replace("\xad", "-")).strip()) or PAGE_NO_RE.match(text):
                    continue
                span = _Span(*s["bbox"], text, bool(s["flags"] & 16) or "bold" in s["font"].lower(), s["size"])
                if m := (CODE_PREFIX_RE.match(text) if span.x0 < CODE_MAX_X else None):   # kod + tanım tek yayılımda
                    rest = m.group(2) or m.group(3)
                    cut = span.x0 + (span.x1 - span.x0) * (len(text) - len(rest)) / len(text)
                    out.append(_Span(span.x0, span.y0, cut - 1, span.y1, m.group(1), span.bold, span.size))
                    span = _Span(max(cut, CODE_MAX_X), span.y0, span.x1, span.y1, rest, span.bold, span.size)
                out.append(span)
    # küçük puntolu "2"/"3" üst simgeleri hemen soldaki birime eklenir ("m" + "3" → "m³")
    for s in [s for s in out if s.size < 8 and s.text in SUPERSCRIPT]:
        base = next((b for b in out if b.size >= 8 and abs(b.x1 - s.x0) < 2.5 and abs(b.yc - s.yc) < 6), None)
        if base is not None:
            base.text, base.x1 = base.text + SUPERSCRIPT[s.text], max(base.x1, s.x1)
            out.remove(s)
    return out


def _is_num(s: _Span) -> bool:
    return bool(NUMBER_RE.match(s.text) or FRACTION_RE.match(s.text) or SCI_RE.match(s.text))


def _lines(spans: list[_Span], right_x: float) -> list[list[_Span]]:
    lines: list[list[_Span]] = []
    for s in sorted(spans, key=lambda s: (s.yc, s.x0)):
        if lines and abs(lines[-1][0].yc - s.yc) <= LINE_TOL:
            lines[-1].append(s)
        else:
            lines.append([s])
    # İki satırlık etiketin ortasına yazılmış değerler ayrı satır gibi görünür: yalnızca sağ sütunlarda
    # sayı içeren satır, sayı içermeyen yakın bir önceki satıra eklenir.
    merged: list[list[_Span]] = []
    for ln in lines:
        prev = merged[-1] if merged else None
        if prev and all(s.x0 >= right_x for s in ln) and any(map(_is_num, ln)) \
                and not any(map(_is_num, prev)) and ln[0].yc - prev[0].yc < 7:
            prev.extend(ln)
        else:
            merged.append(ln)
    return [sorted(ln, key=lambda s: s.x0) for ln in merged]


def _label(text: str) -> tuple[str, re.Match] | None:
    return next(((kind, m) for kind, rx in _LABELS if (m := rx.search(text))), None)


def _is_comp_header(line: list[_Span]) -> bool:
    words = [w.lower() for s in line for w in s.text.split()]
    return bool(words) and not any(s.bold for s in line) and all(w in COMP_HEADER_WORDS for w in words)


def _is_code(s: _Span) -> bool:
    return s.x0 < CODE_MAX_X and bool(CODE_RE.match(s.text))


def _general_price(line: list[_Span]) -> bool:
    """GENEL düzende kalın "1 m² Fiyatı" + değer satırı (kodsuz)."""
    return (any(s.bold and _PRICE_LABEL.search(s.text) and not _is_num(s) for s in line)
            and any(map(_is_num, line)) and not any(map(_is_code, line)))


class _Parser:
    """Sayfa sayfa beslenen durum makinesi: IDLE → HEAD → ITEMS → SUMMARY → NOTES."""

    def __init__(self) -> None:
        self.result: list[ParsedAnalysis] = []
        self.cur: ParsedAnalysis | None = None
        self.state, self.page_no, self.date = "IDLE", 0, None
        # birim ve montaj tutarı sütunlarının sol sınırları (bileşen başlığındaki "Ölçü"/"Montaj"tan)
        self.unit_x, self.montage_x, self.fresh_geometry = 290.0, 500.0, True
        self.section = self.group = self.prev_kind = None
        self.prev_y = 0.0
        self.pending: tuple[str, float, str | None] | None = None  # kodu ortalanmış tanımın ilk satırı
        self.name_parts, self.note_lines = [], []

    def start(self, page: int) -> None:
        self.finish()
        self.cur = ParsedAnalysis(None, "", None, self.date, page=page, end_page=page)
        self.state, self.section, self.group, self.prev_kind, self.pending = "HEAD", None, None, None, None
        self.name_parts, self.note_lines, self.fresh_geometry = [], [], True

    def finish(self) -> None:
        a = self.cur
        if a is None:
            return
        a.name = re.sub(r"\s+", " ", " ".join(self.name_parts)).strip()
        a.notes = "\n".join(self.note_lines).strip() or None
        if self.state != "NOTES" and a.unit_price is None and (a.items or a.subtotal is not None):
            a.warnings.append("Analizin özeti (birim fiyat) bulunamadı; analiz eksik okunmuş ya da sayfa aralığı "
                              "dışında devam ediyor olabilir.")
        check_analysis(a)
        self.result.append(a)
        self.cur, self.state = None, "IDLE"

    def feed_page(self, page_no: int, spans: list[_Span], page_height: float) -> None:
        self.prev_kind, self.pending, self.page_no = None, None, page_no   # tanım devamı sayfa aşmaz
        if not any(s.y0 < 42 and s.bold and "analiz" in s.text.lower() for s in spans):
            return   # analiz sayfası değil (kapak, bölüm kapağı, açıklama)
        for line in _lines(spans, self.unit_x - 10):
            line = self._strip_meta(line, page_height)
            if not line:
                continue
            if any(s.bold for s in line) and "analizin adı" in " ".join(s.text for s in line).lower():
                self.start(page_no)
                continue
            if self.cur is None:
                continue   # ilk analiz başlığından önceki satırlar (aralık dışından devam eden analiz)
            self.cur.end_page = page_no
            {"HEAD": self._head, "ITEMS": self._items, "SUMMARY": self._summary}.get(self.state, self._notes)(line)

    def _strip_meta(self, line: list[_Span], page_height: float) -> list[_Span]:
        """Sayfa başlığı, tarih ve sayfa numarasını ayıklar (tarih kaydedilir)."""
        out = []
        for s in line:
            if iso := (_iso(s.text) if s.x0 > 450 else None):
                self.date = iso
                if self.cur is not None and self.state == "HEAD":
                    self.cur.date = iso
            elif s.y0 < 42 and s.bold and "analiz" in s.text.lower():
                continue  # sayfa üst başlığı: "Genel Fiyat Analizi", "Mekanik Tesisat Birim Fiyat Analizi"
            elif not (s.y0 > page_height - 50 and PAGE_NO_RE.match(s.text)):
                out.append(s)
        return out

    def _head(self, line: list[_Span]) -> None:
        a = self.cur
        if _is_comp_header(line):
            self._geometry(line)
            self.state = "ITEMS"
            return
        for s in line:
            if a.code is None and _is_code(s):
                a.code = s.text
            elif s.x0 > 480 and len(s.text) <= 12 and not _is_num(s):
                a.unit = s.text.rstrip(".") or None
            else:
                self.name_parts.append(s.text)

    def _geometry(self, line: list[_Span]) -> None:
        for s in line:
            low = s.text.lower()
            if low.startswith(("ölçü", "birimi")):
                self.unit_x = s.x0 - 8 if self.fresh_geometry else min(self.unit_x, s.x0 - 8)
                self.fresh_geometry = False
            if low.startswith("montaj"):
                self.montage_x = s.x0 - 4

    def _items(self, line: list[_Span]) -> None:
        a = self.cur
        if _is_comp_header(line):   # sayfa başında tekrarlanan bileşen başlığı
            self._geometry(line)
            return
        if any(_label(s.text) for s in line) or (a.layout == LAYOUT_GENERAL and _general_price(line)):
            self.state = "SUMMARY"
            self._summary(line)
            return
        text = " ".join(s.text for s in line)
        low, y = text.lower(), line[0].yc
        if all(s.bold for s in line) and low.startswith(("malzeme bileşen", "montaj bileşen")):
            a.layout = LAYOUT_MM
            self.group = "Malzeme" if low.startswith("malzeme") else "Montaj"
            self.section, self.prev_kind, self.prev_y = self.group, "group", y
            return
        code = next((s for s in line if _is_code(s)), None)
        right = [s for s in line if s.x0 >= self.unit_x and s is not code]
        nums = [s for s in right if _is_num(s)]
        close = y - self.prev_y <= WRAP_GAP
        if code is None and nums and TOTAL_RE.search(text):
            self.prev_kind, self.prev_y, self.pending = "total", y, None   # ara toplam: bileşen değildir
            return
        if code is not None or nums:
            self._item(line, code, right, nums)
            return
        if self.prev_kind in ("total", "total_cont") and close:
            self.prev_kind, self.prev_y = "total_cont", y   # ara toplam etiketinin devamı
            return
        if self.prev_kind in ("item", "cont") and a.items and \
                (close or (text.startswith("(") and y - self.prev_y <= 14)):
            a.items[-1].description = f"{a.items[-1].description} {text}".strip()
            self.prev_kind = "cont"
        elif self.prev_kind == "heading" and close:
            self.section, self.pending = f"{self.section} {text}", None
        else:
            # Hemen ardından kod satırı gelirse bu satır o bileşenin tanımının ilk satırıdır (_item çözer)
            self.pending = (text, y, self.section)
            self.section = f"{self.group} — {text}" if self.group else text
            self.prev_kind = "heading"
        self.prev_y = y

    def _item(self, line, code, right, nums) -> None:
        a = self.cur
        desc = " ".join(s.text for s in line if s is not code and s.x0 < self.unit_x)
        units = [s for s in right if not _is_num(s)]
        if code is None and units:   # kodsuz satırda sağdaki metin ("Toplam* 0,03") tanıma aittir
            desc, units = f"{desc} {' '.join(s.text for s in units)}", []
        if self.pending and line[0].yc - self.pending[1] <= WRAP_GAP:
            desc, self.section = f"{self.pending[0]} {desc}", self.pending[2]
        self.pending = None
        line_no = len(a.items) + 1
        ref = f"Satır {line_no}" + (f" ({code.text})" if code else "")
        try:
            values = [to_decimal_tr(s.text) for s in nums]
        except ValueError as exc:
            a.warnings.append(f"{ref}: {exc}")
            values = []
        qty = up = amt = qty_text = None
        if len(values) >= 3:
            qty, up, amt = values[-3:]
            qty_text = nums[-3].text
            if len(values) > 3:
                a.warnings.append(f"{ref}: beklenenden fazla sayı ({len(values)}); son üç değer kullanıldı.")
        elif code is None and len(values) == 1:
            amt = values[0]   # kodsuz, yalnızca tutarı olan satır (ör. "%3 (Toplam*0,03)")
        else:
            a.warnings.append(f"{ref}: miktar / birim fiyat / tutar sütunları eksik okundu "
                              f"({', '.join(s.text for s in nums) or 'sayı yok'}).")
            amt = values[-1] if values else None
        column = ("Montaj" if nums[-1].x0 >= self.montage_x else "Malzeme") if a.layout == LAYOUT_MM and nums else None
        if len(units) > 1:
            a.warnings.append(f"{ref}: birim sütununda birden fazla metin: {' '.join(s.text for s in units)}")
        a.items.append(ParsedItem(line_no, self.section, code.text if code else None, desc.strip(),
                                  units[0].text if units else None, qty, up, amt, qty_text, column, self.page_no))
        self.prev_kind, self.prev_y = "item", line[0].yc

    def _summary(self, line: list[_Span]) -> None:
        a = self.cur
        labels: list[tuple[_Span, str, re.Match | None]] = []
        for s in line:
            found = None if _is_num(s) else _label(s.text)
            if found:
                labels.append((s, *found))
                if found[0] in ("overhead", "rate"):
                    a.overhead_rate = to_decimal_tr(found[1].group(1)) / 100
                a.layout = LAYOUT_MM if found[0] == "net_split" else a.layout
            elif a.layout == LAYOUT_GENERAL and not _is_num(s) and _PRICE_LABEL.search(s.text) \
                    and (s.bold or any(map(_is_num, line))):
                labels.append((s, "unit_price", None))
        if not labels:
            self._notes(line)
            return
        values: dict[int, list[tuple[Decimal, float]]] = {}
        for n in filter(_is_num, line):   # her değer, solundaki en yakın (dikeyde) etikete aittir
            cands = [(abs(lab.yc - n.yc), i) for i, (lab, kind, _) in enumerate(labels)
                     if lab.x1 <= n.x0 + 1 and kind not in ("ignore", "rate")]
            if cands:
                values.setdefault(min(cands)[1], []).append((to_decimal_tr(n.text), n.x0))
            else:
                a.warnings.append(f"Özet satırında etiketsiz değer: {n.text}")
        for i, (lab, kind, _) in enumerate(labels):
            pairs = values.get(i, [])
            vals = [v for v, _ in pairs]
            if kind == "unit_price":
                self.state = "NOTES"   # değer olmasa da özet biter (ör. formülle verilen analizler)
            if kind in ("ignore", "rate"):
                continue
            if not vals:
                if not (kind == "montage" and a.labour_total is None):   # montajsız analizde boş kalır
                    a.warnings.append(f"Özet etiketi için değer bulunamadı: {lab.text}")
            elif kind == "net_split":
                if len(vals) >= 2:
                    a.material_total, a.labour_total = vals[0], vals[1]
                elif pairs[0][1] >= self.montage_x:   # tek değer: sütun konumundan
                    a.labour_total = vals[0]
                else:
                    a.material_total = vals[0]
            else:
                setattr(a, {"subtotal": "subtotal", "overhead": "overhead_amount", "montage": "montage_price",
                            "unit_price": "unit_price"}[kind], vals[-1])

    def _notes(self, line: list[_Span]) -> None:
        text = " ".join(s.text for s in line).strip()
        if not text or (_label(text) or ("",))[0] in ("rate", "ignore"):
            return
        if self.note_lines and not NOTE_BREAK_RE.match(text) and not self.note_lines[-1].endswith(":"):
            self.note_lines[-1] = f"{self.note_lines[-1]} {text}"
        else:
            self.note_lines.append(text)


def _near(a: Decimal, b: Decimal, tol: Decimal = TOL) -> bool:
    return abs(a - b) <= tol


def check_analysis(a: ParsedAnalysis) -> None:
    """Aritmetik denetim. Uyuşmazlıklar `warnings`'e eklenir; hiçbir değer düzeltilmez."""
    w = a.warnings
    if not a.code:
        w.append("Analizin poz numarası bulunamadı.")
    if not a.items and a.subtotal is None and a.unit_price is None:
        w.append("Analiz sayısal değer içermiyor (fiyatı formülle ya da başka pozlardan hesaplanan analiz "
                 "olabilir); birim fiyat okunamadı.")
        return
    w.extend([] if a.items else ["Bileşen satırı bulunamadı."])
    for it in a.items:
        if None not in (it.quantity, it.unit_price, it.amount) and \
                not _near(it.quantity * it.unit_price, it.amount):
            w.append(f"Satır {it.line_no} ({it.code or it.description[:30]}): miktar × birim fiyat = "
                     f"{it.quantity * it.unit_price} ≠ tutar {it.amount}")
    total = sum((it.amount for it in a.items if it.amount is not None), Decimal(0))
    tol_sum = TOL * max(1, len(a.items))
    if a.subtotal is None and a.overhead_rate is None and a.unit_price is not None and a.items:
        # kârsız yardımcı analiz (ör. makine saat ücreti): birim fiyat doğrudan bileşen toplamıdır
        if not _near(total, a.unit_price, tol_sum):
            w.append(f"Bileşen tutarları toplamı {total} ≠ birim fiyat {a.unit_price} (kârsız analiz)")
        return
    if a.subtotal is None:
        w.append("Kârsız toplam (Malzeme + İşçilik / Kârsız Malzeme ve Montaj) bulunamadı.")
    elif a.items and not _near(total, a.subtotal, tol_sum):
        w.append(f"Bileşen tutarları toplamı {total} ≠ kârsız toplam {a.subtotal}")
    if a.layout == LAYOUT_MM:
        for col, part_total in (("Malzeme", a.material_total), ("Montaj", a.labour_total)):
            part = sum((it.amount for it in a.items if it.column == col and it.amount is not None), Decimal(0))
            if part_total is not None and not _near(part, part_total, tol_sum):
                w.append(f"Kârsız {col.lower()} toplamı {part_total} ≠ {col.lower()} sütunu toplamı {part}")
        if None not in (a.material_total, a.labour_total, a.subtotal) and \
                not _near(a.material_total + a.labour_total, a.subtotal):
            w.append(f"Kârsız malzeme + montaj {a.material_total + a.labour_total} ≠ toplam {a.subtotal}")
        if a.montage_price is None and a.labour_total is not None:
            w.append("Montaj bedeli bulunamadı.")
    if a.overhead_rate is None:
        w.append("Yüklenici kârı ve genel gider oranı bulunamadı.")
    elif a.subtotal is not None:
        k = 1 + a.overhead_rate
        if a.unit_price is not None and not _near(a.subtotal * k, a.unit_price):
            w.append(f"Kârsız toplam × {k} = {a.subtotal * k} ≠ birim fiyat {a.unit_price}")
        if a.overhead_amount is not None and not _near(a.subtotal * a.overhead_rate, a.overhead_amount):
            w.append(f"Kâr ve genel gider tutarı {a.overhead_amount} ≠ {a.subtotal * a.overhead_rate}")
        if a.layout == LAYOUT_MM and None not in (a.labour_total, a.montage_price) and \
                not _near(a.labour_total * k, a.montage_price):
            w.append(f"Montaj bedeli {a.montage_price} ≠ kârsız montaj × {k} = {a.labour_total * k}")
    if None not in (a.overhead_amount, a.subtotal, a.unit_price) and \
            not _near(a.subtotal + a.overhead_amount, a.unit_price):
        w.append(f"Kârsız toplam + kâr {a.subtotal + a.overhead_amount} ≠ birim fiyat {a.unit_price}")


def parse_analysis_book(path: Path | str, pages: Iterable[int] | None = None,
                        progress: Callable[[int, int], None] | None = None,
                        cancelled: Callable[[], bool] | None = None) -> list[ParsedAnalysis]:
    """`pages`: 1'den başlayan sayfa no'ları (None → tümü). İptalde `AnalysisParseCancelled` yükselir."""
    doc = open_pdf(Path(path))
    parser = _Parser()
    try:
        selected = list(range(1, doc.page_count + 1)) if pages is None else \
            [p for p in pages if 1 <= p <= doc.page_count]
        for n, no in enumerate(selected, start=1):
            if cancelled and cancelled():
                raise AnalysisParseCancelled("Analiz kitabı okuma işlemi kullanıcı tarafından iptal edildi.")
            page = doc[no - 1]
            parser.feed_page(no, _page_spans(page), page.rect.height)
            if progress:
                progress(n, len(selected))
        parser.finish()
    finally:
        doc.close()
    return parser.result
