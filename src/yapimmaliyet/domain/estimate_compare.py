"""İki keşfin (ör. ihale keşfi ↔ gerçekleşen/tamamlanma keşfi) kalem bazında karşılaştırması — saf hesap.

Eşleştirme kuralları:
- Otomatik eşleşme yalnızca HER İKİ keşifte de TEK kez geçen aynı (normalize) poz kodu için yapılır.
- Kodu değişmiş görünen (aynı tarif, farklı kod) ya da birden çok kez geçen kodlar otomatik birleştirilmez;
  öneri olarak sunulur ve ancak kullanıcı onayıyla (decisions) eşleşir.
- Fiyatı olmayan kalemin tutarı bilinmez: "çözümlenmemiş" sayılır, toplamlara katılmaz, ayrıca sayılır.

Fark ayrıştırması (sıralamadan bağımsız): miktar etkisi = (q2−q1)·p1, fiyat etkisi = (p2−p1)·q1,
etkileşim = (q2−q1)·(p2−p1); toplam = q2p2 − q1p1. Satır tutarları hesap motorunda yuvarlandığından tutar farkı ile
üç terimin toplamı arasında kuruş düzeyinde yuvarlama farkı olabilir; bu fark ayrıca gösterilir.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit

ZERO = Decimal(0)
METHOD_NOTE = ("Miktar etkisi = (son miktar − ilk miktar) × ilk birim fiyat; fiyat etkisi = (son fiyat − ilk fiyat) × "
               "ilk miktar; etkileşim = miktar farkı × fiyat farkı. Üçünün toplamı son tutar − ilk tutar'a eşittir "
               "(satır yuvarlaması dışında).")
LEGAL_NOTE = ("Bu karşılaştırma bilgi amaçlıdır: hakediş, sözleşme değişikliği ya da yeni birim fiyat kararı değildir. "
              "İhale/sözleşme fiyatı, yeni birim fiyat ve güncel ÇŞB fiyatı farklı kavramlardır; hangi fiyatın "
              "uygulanacağı sözleşme ve mevzuata göre ayrıca doğrulanmalıdır.")


@dataclass(frozen=True)
class Line:
    item_id: int
    line_no: int
    code: str | None
    description: str | None
    unit: str | None
    quantity: Decimal
    unit_price: Decimal | None      # hesap motorunun kullandığı fiyat (yoksa None)
    amount: Decimal | None
    source: str = ""                # fiyat kaynağı ve dönemi

    @property
    def code_key(self) -> str:
        return normalize_code(self.code) if self.code else ""

    @property
    def desc_key(self) -> str:
        return normalize_search(self.description or "")


class RowStatus(StrEnum):
    AYNI = "AYNI"
    ARTAN = "ARTAN"
    AZALAN = "AZALAN"
    FIYAT = "FIYAT"
    KOD_DEGISEN = "KOD_DEGISEN"
    YENI = "YENI"
    CIKAN = "CIKAN"

    @property
    def label(self) -> str:
        return {"AYNI": "Aynı", "ARTAN": "Miktarı artan", "AZALAN": "Miktarı azalan", "FIYAT": "Yalnız fiyatı değişen",
                "KOD_DEGISEN": "Kodu/tarifi değişen (onaylı)", "YENI": "Yeni eklenen",
                "CIKAN": "Çıkarılan"}[self.value]


@dataclass(frozen=True)
class Row:
    status: RowStatus
    first: Line | None
    second: Line | None
    basis: str                       # eşleştirme gerekçesi
    unresolved: bool = False         # bir tarafın fiyatı yok → tutar farkı bilinmiyor

    def _q(self, ln):
        return ln.quantity if ln else ZERO

    @property
    def qty_diff(self) -> Decimal:
        return self._q(self.second) - self._q(self.first)

    @property
    def qty_ratio(self) -> Decimal | None:
        if not self.first or not self.second or not self.first.quantity:
            return None
        return self.second.quantity / self.first.quantity - 1

    @property
    def price_diff(self) -> Decimal | None:
        if not self.first or not self.second or None in (self.first.unit_price, self.second.unit_price):
            return None
        return self.second.unit_price - self.first.unit_price

    @property
    def price_ratio(self) -> Decimal | None:
        d = self.price_diff
        return None if d is None or not self.first.unit_price else d / self.first.unit_price

    @property
    def amount_diff(self) -> Decimal | None:
        if self.unresolved:
            return None
        return (self.second.amount if self.second else ZERO) - (self.first.amount if self.first else ZERO)

    def effects(self) -> tuple[Decimal, Decimal, Decimal] | None:
        """(miktar etkisi, fiyat etkisi, etkileşim); yalnızca iki tarafı da fiyatlı eşleşmiş kalemlerde."""
        if self.unresolved or not self.first or not self.second:
            return None
        q1, p1, q2, p2 = self.first.quantity, self.first.unit_price, self.second.quantity, self.second.unit_price
        return (q2 - q1) * p1, (p2 - p1) * q1, (q2 - q1) * (p2 - p1)


@dataclass(frozen=True)
class Suggestion:
    first: Line
    second: Line
    reason: str


@dataclass
class Summary:
    first_total: Decimal = ZERO
    second_total: Decimal = ZERO
    increased: Decimal = ZERO         # miktarı artan kalemlerin tutar farkı toplamı
    decreased: Decimal = ZERO         # miktarı azalan kalemlerin tutar farkı toplamı (negatif)
    added: Decimal = ZERO             # yeni eklenen kalemlerin tutarı
    removed: Decimal = ZERO           # çıkarılan kalemlerin tutarı (pozitif yazılır; fark olarak −)
    price_only: Decimal = ZERO        # yalnız fiyatı değişen kalemlerin farkı
    qty_effect: Decimal = ZERO
    price_effect: Decimal = ZERO
    interaction: Decimal = ZERO
    unresolved: int = 0
    counts: dict[RowStatus, int] = field(default_factory=dict)

    @property
    def difference(self) -> Decimal:
        return self.second_total - self.first_total

    @property
    def ratio(self) -> Decimal | None:
        return None if not self.first_total else self.difference / self.first_total


@dataclass
class Comparison:
    rows: list[Row]
    suggestions: list[Suggestion]
    summary: Summary
    notes: list[str]


def _status(f: Line, s: Line) -> RowStatus:
    if s.quantity > f.quantity:
        return RowStatus.ARTAN
    if s.quantity < f.quantity:
        return RowStatus.AZALAN
    if f.unit_price is not None and s.unit_price is not None and f.unit_price != s.unit_price:
        return RowStatus.FIYAT
    return RowStatus.AYNI


def compare(first: list[Line], second: list[Line], approved: set[tuple[int, int]] = frozenset(),
            rejected: set[tuple[int, int]] = frozenset()) -> Comparison:
    """`approved`/`rejected`: kullanıcının (ilk kalem, son kalem) eşleştirme kararları."""
    rows: list[Row] = []
    used_f: set[int] = set()
    used_s: set[int] = set()
    by_id_f, by_id_s = {x.item_id: x for x in first}, {x.item_id: x for x in second}
    for fi, si in sorted(approved):                         # kullanıcı onaylı eşleşmeler önce
        f, s = by_id_f.get(fi), by_id_s.get(si)
        if f and s and fi not in used_f and si not in used_s:
            st = RowStatus.KOD_DEGISEN if f.code_key != s.code_key else _status(f, s)
            rows.append(Row(st, f, s, "Kullanıcı onaylı eşleştirme", f.amount is None or s.amount is None))
            used_f.add(fi)
            used_s.add(si)
    cf, cs = Counter(x.code_key for x in first if x.code_key), Counter(x.code_key for x in second if x.code_key)
    s_by_code = {x.code_key: x for x in second if x.code_key and cs[x.code_key] == 1}
    for f in first:
        if f.item_id in used_f or not f.code_key or cf[f.code_key] != 1:
            continue
        s = s_by_code.get(f.code_key)
        if s is None or s.item_id in used_s:
            continue
        basis = "Aynı poz kodu (iki keşifte de tek)"
        if normalize_unit(f.unit or "") != normalize_unit(s.unit or ""):
            basis += f"; BİRİM FARKLI ({f.unit} / {s.unit}) — miktarlar doğrudan karşılaştırılamaz"
        rows.append(Row(_status(f, s), f, s, basis, f.amount is None or s.amount is None))
        used_f.add(f.item_id)
        used_s.add(s.item_id)
    rest_f = [x for x in first if x.item_id not in used_f]
    rest_s = [x for x in second if x.item_id not in used_s]
    suggestions = []
    for f in rest_f:
        for s in rest_s:
            if (f.item_id, s.item_id) in rejected:
                continue
            if f.code_key and f.code_key == s.code_key:
                suggestions.append(Suggestion(f, s, "Aynı kod birden fazla kez geçiyor; hangi satırların eşleştiği "
                                                    "belirsiz"))
            elif f.desc_key and f.desc_key == s.desc_key:
                suggestions.append(Suggestion(f, s, "Tarif aynı, kod farklı"))
    for f in rest_f:
        rows.append(Row(RowStatus.CIKAN, f, None, "Son keşifte karşılığı yok", f.amount is None))
    for s in rest_s:
        rows.append(Row(RowStatus.YENI, None, s, "İlk keşifte karşılığı yok", s.amount is None))
    rows.sort(key=lambda r: ((r.first or r.second).line_no if r.first else 10_000 + r.second.line_no))
    return Comparison(rows, suggestions, _summary(first, second, rows), [METHOD_NOTE, LEGAL_NOTE])


def _summary(first: list[Line], second: list[Line], rows: list[Row]) -> Summary:
    sm = Summary(first_total=sum((x.amount for x in first if x.amount is not None), ZERO),
                 second_total=sum((x.amount for x in second if x.amount is not None), ZERO))
    for r in rows:
        sm.counts[r.status] = sm.counts.get(r.status, 0) + 1
        if r.unresolved:
            sm.unresolved += 1
            continue
        d = r.amount_diff
        if r.status is RowStatus.YENI:
            sm.added += d
        elif r.status is RowStatus.CIKAN:
            sm.removed += -d
        elif r.status is RowStatus.ARTAN:
            sm.increased += d
        elif r.status is RowStatus.AZALAN:
            sm.decreased += d
        elif r.status is RowStatus.FIYAT:
            sm.price_only += d
        eff = r.effects()
        if eff:
            sm.qty_effect += eff[0]
            sm.price_effect += eff[1]
            sm.interaction += eff[2]
    return sm
