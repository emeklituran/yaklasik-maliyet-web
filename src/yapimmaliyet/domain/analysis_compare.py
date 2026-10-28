"""İki birim fiyat analizinin girdi bazında karşılaştırması (saf hesap; veritabanı bilmez).

Eşleme: girdi kodu (rayiç/poz kodu) varsa koda göre, yoksa normalize tanıma göre. Eşleşmeyen girdi "yeni" ya da
"çıkan" sayılır; benzer ama farklı girdiler birleştirilmez.

Fark ayrıştırması (sıralamadan bağımsız, üç terim):
    miktar etkisi   = (q₂ − q₁) × p₁
    fiyat etkisi    = (p₂ − p₁) × q₁
    etkileşim       = (q₂ − q₁) × (p₂ − p₁)
    toplam          = q₂p₂ − q₁p₁ = miktar etkisi + fiyat etkisi + etkileşim
Kaynaktaki tutar q×p'den farklıysa (yuvarlama ya da katsayı) aradaki fark "kaynak tutar farkı" olarak ayrıca
gösterilir. Birimleri farklı girdilerde etki hesaplanmaz (dönüşüm kuralı bilinmez).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit

METHOD_NOTE = ("Ayrıştırma: miktar etkisi = (q2−q1)×p1; fiyat etkisi = (p2−p1)×q1; etkileşim = (q2−q1)×(p2−p1). "
               "Üç terimin toplamı q2×p2 − q1×p1'e eşittir; terimler sıralamadan bağımsızdır.")


class Status(StrEnum):
    AYNI = "AYNI"
    DEGISTI = "DEGISTI"
    YENI = "YENI"
    CIKAN = "CIKAN"
    BIRIM = "BIRIM"

    @property
    def label(self) -> str:
        return {"AYNI": "Aynı", "DEGISTI": "Değişti", "YENI": "Yalnız 2. analizde", "CIKAN": "Yalnız 1. analizde",
                "BIRIM": "Birim farklı"}[self.value]


@dataclass(frozen=True)
class Component:
    code: str | None
    description: str
    section: str | None
    unit: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None

    @property
    def key(self) -> str:
        return "K:" + normalize_code(self.code) if self.code else "T:" + normalize_search(self.description)


@dataclass(frozen=True)
class CompareRow:
    status: Status
    code: str | None
    description: str
    section: str | None
    a: Component | None
    b: Component | None
    qty_effect: Decimal | None = None
    price_effect: Decimal | None = None
    interaction: Decimal | None = None

    @property
    def amount_diff(self) -> Decimal:
        return (self.b.amount or Decimal(0) if self.b else Decimal(0)) - \
            (self.a.amount or Decimal(0) if self.a else Decimal(0))

    @property
    def source_residual(self) -> Decimal | None:
        """Kaynak tutar farkı ile q×p ayrıştırması arasındaki fark (yuvarlama/katsayı)."""
        if self.qty_effect is None:
            return None
        return self.amount_diff - (self.qty_effect + self.price_effect + self.interaction)


@dataclass
class AnalysisComparison:
    rows: list[CompareRow]
    total_a: Decimal | None
    total_b: Decimal | None
    sections: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total_diff(self) -> Decimal | None:
        return None if self.total_a is None or self.total_b is None else self.total_b - self.total_a

    def effect_totals(self) -> tuple[Decimal, Decimal, Decimal]:
        rows = [r for r in self.rows if r.qty_effect is not None]
        return (sum((r.qty_effect for r in rows), Decimal(0)), sum((r.price_effect for r in rows), Decimal(0)),
                sum((r.interaction for r in rows), Decimal(0)))

    def counts(self) -> dict[Status, int]:
        out = {s: 0 for s in Status}
        for r in self.rows:
            out[r.status] += 1
        return out


def _index(items: list[Component]) -> tuple[OrderedDict[str, Component], list[str]]:
    out: OrderedDict[str, Component] = OrderedDict()
    dups = []
    for c in items:
        if c.key in out:
            dups.append(c.code or c.description)
        else:
            out[c.key] = c
    return out, dups


def compare(a: list[Component], b: list[Component], total_a: Decimal | None = None,
            total_b: Decimal | None = None) -> AnalysisComparison:
    ia, dup_a = _index(a)
    ib, dup_b = _index(b)
    rows: list[CompareRow] = []
    for key in list(ia) + [k for k in ib if k not in ia]:
        x, y = ia.get(key), ib.get(key)
        ref = x or y
        if x is None or y is None:
            rows.append(CompareRow(Status.YENI if x is None else Status.CIKAN, ref.code, ref.description, ref.section,
                                   x, y))
            continue
        if normalize_unit(x.unit or "") != normalize_unit(y.unit or ""):
            rows.append(CompareRow(Status.BIRIM, ref.code, ref.description, ref.section, x, y))
            continue
        q1, p1, q2, p2 = x.quantity, x.unit_price, y.quantity, y.unit_price
        if None in (q1, p1, q2, p2):
            same = x.amount == y.amount
            rows.append(CompareRow(Status.AYNI if same else Status.DEGISTI, ref.code, ref.description, ref.section,
                                   x, y))
            continue
        same = q1 == q2 and p1 == p2
        rows.append(CompareRow(Status.AYNI if same else Status.DEGISTI, ref.code, ref.description, ref.section, x, y,
                               (q2 - q1) * p1, (p2 - p1) * q1, (q2 - q1) * (p2 - p1)))
    sections: dict[str, tuple[Decimal, Decimal]] = {}
    for r in rows:
        name = r.section or "Diğer"
        sa, sb = sections.get(name, (Decimal(0), Decimal(0)))
        sections[name] = (sa + (r.a.amount or Decimal(0) if r.a else Decimal(0)),
                          sb + (r.b.amount or Decimal(0) if r.b else Decimal(0)))
    notes = [METHOD_NOTE]
    if dup_a or dup_b:
        notes.append("Aynı kodlu/tanımlı girdi bir analizde birden fazla kez geçiyor; yalnızca ilki karşılaştırıldı: "
                     + ", ".join(sorted(set(dup_a + dup_b)))[:300])
    if any(r.status is Status.BIRIM for r in rows):
        notes.append("Birimi farklı girdilerde miktar/fiyat etkisi hesaplanmadı (dönüşüm kuralı bilinmiyor).")
    return AnalysisComparison(rows, total_a, total_b, sections, notes)
