"""Özel poz analiz hesabı.

Girdi tutarı   = miktar × katsayı × birim dönüşümü × (1 + fire) × girdi birim fiyatı  (2 basamağa yuvarlanır)
Ara toplam     = Σ girdi tutarları
Genel gider    = ara toplam × genel gider oranı
Kâr            = ara toplam × kâr oranı                      (kâr tabanı: ARA_TOPLAM — toplamalı)
               = (ara toplam + genel gider) × kâr oranı       (kâr tabanı: ARA_TOPLAM_VE_GG — bileşik)
Analiz fiyatı  = (ara toplam + genel gider + kâr) ÷ analiz baz miktarı (birim fiyat basamağına yuvarlanır)

Oranlar kesir olarak verilir (0,15 = %15). Oranların kaynağı analizde ayrıca yazılır; burada
hiçbir oran varsayılan mevzuat kuralı olarak kabul edilmez.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.money import CALC_CONTEXT, to_decimal
from yapimmaliyet.domain.rounding import DEFAULT_POLICY, RoundingPolicy, quantize


class ItemType(StrEnum):
    MALZEME = "MALZEME"
    ISCILIK = "ISCILIK"
    MAKINE = "MAKINE"
    NAKLIYE = "NAKLIYE"
    DIGER = "DIGER"

    @property
    def label(self) -> str:
        return {"MALZEME": "Malzeme", "ISCILIK": "İşçilik", "MAKINE": "Makine ve ekipman",
                "NAKLIYE": "Nakliye", "DIGER": "Diğer"}[self.value]


class ProfitBase(StrEnum):
    ARA_TOPLAM = "ARA_TOPLAM"
    ARA_TOPLAM_VE_GG = "ARA_TOPLAM_VE_GG"

    @property
    def label(self) -> str:
        return ("Kâr ara toplam üzerinden (toplamalı)" if self is ProfitBase.ARA_TOPLAM
                else "Kâr ara toplam + genel gider üzerinden (bileşik)")


@dataclass(frozen=True)
class AnalysisItemInput:
    item_type: ItemType
    description: str
    unit: str
    quantity: Decimal
    unit_price: Decimal
    waste_rate: Decimal = Decimal(0)
    coefficient: Decimal = Decimal(1)
    unit_conversion: Decimal = Decimal(1)
    source_text: str | None = None
    ref_position_price_id: int | None = None
    ref_custom_position_id: int | None = None  # alt analiz: başka bir özel poz


@dataclass(frozen=True)
class AnalysisSpec:
    base_quantity: Decimal
    overhead_rate: Decimal
    profit_rate: Decimal
    profit_base: ProfitBase
    rule_source_text: str
    items: tuple[AnalysisItemInput, ...]


@dataclass(frozen=True)
class AnalysisLine:
    item: AnalysisItemInput
    effective_quantity: Decimal
    amount: Decimal


@dataclass(frozen=True)
class AnalysisResult:
    lines: tuple[AnalysisLine, ...]
    subtotals: dict[ItemType, Decimal]
    subtotal: Decimal
    overhead: Decimal
    profit: Decimal
    total: Decimal
    unit_price: Decimal
    explanation: list[str] = field(default_factory=list)


def validate_spec(spec: AnalysisSpec) -> list[str]:
    errors = []
    if not spec.items:
        errors.append("Analizde en az bir girdi olmalıdır.")
    if to_decimal(spec.base_quantity) <= 0:
        errors.append("Analiz baz miktarı sıfırdan büyük olmalıdır.")
    for name, rate in (("Genel gider oranı", spec.overhead_rate), ("Kâr oranı", spec.profit_rate)):
        r = to_decimal(rate)
        if r < 0 or r > 1:
            errors.append(f"{name} %0 ile %100 arasında olmalıdır.")
    for i, it in enumerate(spec.items, start=1):
        if not (it.description or "").strip():
            errors.append(f"{i}. girdinin açıklaması boş.")
        if not (it.unit or "").strip():
            errors.append(f"{i}. girdinin birimi boş.")
        if it.ref_position_price_id is not None and it.ref_custom_position_id is not None:
            errors.append(f"{i}. girdi hem kütüphane fiyatına hem alt analize bağlanamaz.")
        for label, v in (("miktarı", it.quantity), ("birim fiyatı", it.unit_price), ("katsayısı", it.coefficient),
                         ("birim dönüşümü", it.unit_conversion)):
            if to_decimal(v) < 0:
                errors.append(f"{i}. girdinin {label} negatif olamaz.")
        if not 0 <= to_decimal(it.waste_rate) < 1:
            errors.append(f"{i}. girdinin fire oranı %0 ile %100 arasında olmalıdır.")
    return errors


def compute(spec: AnalysisSpec, policy: RoundingPolicy = DEFAULT_POLICY) -> AnalysisResult:
    errors = validate_spec(spec)
    if errors:
        raise ValueError("\n".join(errors))
    c = CALC_CONTEXT
    lines = []
    subtotals = {t: Decimal(0) for t in ItemType}
    for it in spec.items:
        eff = c.multiply(c.multiply(c.multiply(to_decimal(it.quantity), to_decimal(it.coefficient)),
                                    to_decimal(it.unit_conversion)), c.add(Decimal(1), to_decimal(it.waste_rate)))
        amount = quantize(c.multiply(eff, to_decimal(it.unit_price)), policy.amount_places, policy.mode)
        lines.append(AnalysisLine(it, eff, amount))
        subtotals[ItemType(it.item_type)] += amount
    subtotal = sum((ln.amount for ln in lines), Decimal(0))
    overhead = quantize(c.multiply(subtotal, to_decimal(spec.overhead_rate)), policy.amount_places, policy.mode)
    profit_base = subtotal if spec.profit_base is ProfitBase.ARA_TOPLAM else subtotal + overhead
    profit = quantize(c.multiply(profit_base, to_decimal(spec.profit_rate)), policy.amount_places, policy.mode)
    total = subtotal + overhead + profit
    unit_price = quantize(c.divide(total, to_decimal(spec.base_quantity)), policy.price_places, policy.mode)
    pct = lambda r: f"%{to_decimal(r) * 100:.2f}".replace(".", ",")  # noqa: E731
    explanation = [
        f"Ara toplam {subtotal} TL ({len(lines)} girdi).",
        f"Genel gider {pct(spec.overhead_rate)} × ara toplam = {overhead} TL.",
        f"Kâr {pct(spec.profit_rate)} × {'ara toplam' if spec.profit_base is ProfitBase.ARA_TOPLAM else '(ara toplam + genel gider)'} = {profit} TL.",
        f"Toplam {total} TL ÷ baz miktar {spec.base_quantity} = birim fiyat {unit_price} TL.",
        f"Oranların dayanağı: {spec.rule_source_text or 'belirtilmemiş'}.",
    ]
    return AnalysisResult(tuple(lines), subtotals, subtotal, overhead, profit, total, unit_price, explanation)
