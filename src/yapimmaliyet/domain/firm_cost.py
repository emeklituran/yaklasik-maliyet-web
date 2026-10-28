"""Firma maliyeti (C) ve teklif senaryoları (D).

Firma maliyeti:
- Satır doğrudan birim maliyeti = malzeme + işçilik + makine + nakliye + taşeron + diğer (birim başına)
- Satır doğrudan tutarı = round(miktar × doğrudan birim maliyet, 2)
- Dolaylı giderler (şantiye, finansman, teminat, sigorta, sözleşme giderleri vb.) sabit tutar ya da
  doğrudan maliyet toplamına oran olarak girilir ve satırlara doğrudan tutarları oranında dağıtılır.
  Yuvarlama artığı en büyük satıra eklenir; dağıtılan toplam her zaman dolaylı gider toplamına eşittir.
Teklif senaryosu:
- Teklif birim fiyatı = round(birim toplam maliyet × (1 + risk) × (1 + kâr), 2)
- Teklif tutarı = Σ round(miktar × teklif birim fiyatı, 2)
Oranlar kesirdir (0,10 = %10). Bunlar firma tercihidir; mevzuat kuralı değildir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.money import CALC_CONTEXT, to_decimal
from yapimmaliyet.domain.rounding import DEFAULT_POLICY, RoundingPolicy, line_amount, quantize

COMPONENTS = ("material", "labour", "machine", "transport", "subcontract", "other")
COMPONENT_LABELS = {"material": "Malzeme", "labour": "İşçilik", "machine": "Makine", "transport": "Nakliye",
                    "subcontract": "Taşeron", "other": "Diğer"}
ZERO = Decimal("0.00")


class IndirectMethod(StrEnum):
    SABIT = "SABIT"
    ORAN = "ORAN"

    @property
    def label(self) -> str:
        return "Sabit tutar (TL)" if self is IndirectMethod.SABIT else "Doğrudan maliyete oran (%)"


@dataclass(frozen=True)
class CostLineInput:
    item_id: int
    line_no: int
    work_group: str | None
    code: str | None
    description: str | None
    unit: str | None
    quantity: Decimal
    components: dict[str, Decimal]   # birim başına; eksik bileşen 0 sayılır

    @property
    def direct_unit(self) -> Decimal:
        return sum((to_decimal(self.components.get(k, Decimal(0))) for k in COMPONENTS), Decimal(0))


@dataclass(frozen=True)
class IndirectCost:
    name: str
    method: IndirectMethod
    value: Decimal
    note: str | None = None


@dataclass(frozen=True)
class CostLine:
    input: CostLineInput
    direct_unit: Decimal
    direct_amount: Decimal
    indirect_amount: Decimal
    total_cost: Decimal
    unit_total_cost: Decimal          # yuvarlanmamış (teklif fiyatı için)


@dataclass(frozen=True)
class CostResult:
    lines: tuple[CostLine, ...]
    direct_total: Decimal
    indirect_items: tuple[tuple[IndirectCost, Decimal], ...]
    indirect_total: Decimal
    total_cost: Decimal
    warnings: list[str] = field(default_factory=list)


def compute_cost(lines: list[CostLineInput], indirects: list[IndirectCost],
                 policy: RoundingPolicy = DEFAULT_POLICY) -> CostResult:
    for ind in indirects:
        v = to_decimal(ind.value)
        if v < 0:
            raise ValueError(f"'{ind.name}' dolaylı gideri negatif olamaz.")
        if ind.method is IndirectMethod.ORAN and v > 1:
            raise ValueError(f"'{ind.name}' oranı %100'ü aşamaz.")
    for ln in lines:
        if any(to_decimal(v) < 0 for v in ln.components.values()):
            raise ValueError(f"Sıra {ln.line_no}: maliyet bileşeni negatif olamaz.")
    ordered = sorted(lines, key=lambda x: x.line_no)
    direct = [(ln, ln.direct_unit, line_amount(ln.quantity, ln.direct_unit, policy)) for ln in ordered]
    direct_total = sum((d[2] for d in direct), Decimal(0))
    ind_items = []
    for ind in indirects:
        amount = (quantize(ind.value, policy.amount_places, policy.mode) if ind.method is IndirectMethod.SABIT
                  else quantize(CALC_CONTEXT.multiply(direct_total, to_decimal(ind.value)), policy.amount_places,
                                policy.mode))
        ind_items.append((ind, amount))
    indirect_total = sum((a for _, a in ind_items), Decimal(0))
    warnings = []
    shares = [ZERO] * len(direct)
    if indirect_total and direct_total:
        for i, (_, _, amt) in enumerate(direct):
            shares[i] = quantize(CALC_CONTEXT.divide(CALC_CONTEXT.multiply(indirect_total, amt), direct_total),
                                 policy.amount_places, policy.mode)
        residual = indirect_total - sum(shares, Decimal(0))
        if residual:
            biggest = max(range(len(direct)), key=lambda i: direct[i][2])
            shares[biggest] += residual
    elif indirect_total:
        warnings.append("Doğrudan maliyet girilmediği için dolaylı giderler satırlara dağıtılamadı.")
    out = []
    for (ln, unit, amt), share in zip(direct, shares):
        total = amt + share
        unit_total = CALC_CONTEXT.divide(total, ln.quantity) if ln.quantity else unit
        out.append(CostLine(ln, unit, amt, share, total, unit_total))
    missing = [ln.input.line_no for ln in out if ln.direct_unit == 0]
    if missing:
        warnings.append(f"{len(missing)} kalemde maliyet girilmemiş (ör. sıra {missing[0]}); bu kalemler teklifte "
                        "sıfır fiyatla yer alır.")
    return CostResult(tuple(out), direct_total, tuple(ind_items), indirect_total, direct_total + indirect_total,
                      warnings)


@dataclass(frozen=True)
class Scenario:
    name: str
    profit_rate: Decimal
    risk_rate: Decimal = Decimal(0)
    notes: str | None = None


@dataclass(frozen=True)
class OfferLine:
    cost: CostLine
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class OfferResult:
    scenario: Scenario
    lines: tuple[OfferLine, ...]
    cost_total: Decimal
    offer_total: Decimal
    profit_amount: Decimal
    margin: Decimal | None            # kâr / teklif tutarı
    ratio_to_reference: Decimal | None


def compute_offer(cost: CostResult, scenario: Scenario, reference_total: Decimal | None = None,
                  policy: RoundingPolicy = DEFAULT_POLICY) -> OfferResult:
    for label, r in (("Kâr", scenario.profit_rate), ("Risk", scenario.risk_rate)):
        if not -1 < to_decimal(r) <= 5:
            raise ValueError(f"{label} oranı geçersiz: {r}")
    factor = CALC_CONTEXT.multiply(1 + to_decimal(scenario.risk_rate), 1 + to_decimal(scenario.profit_rate))
    lines = []
    for c in cost.lines:
        unit_price = quantize(CALC_CONTEXT.multiply(c.unit_total_cost, factor), policy.price_places, policy.mode)
        lines.append(OfferLine(c, unit_price, line_amount(c.input.quantity, unit_price, policy)))
    offer_total = sum((ln.amount for ln in lines), Decimal(0))
    profit = offer_total - cost.total_cost
    margin = quantize(profit / offer_total, 4) if offer_total else None
    ratio = quantize(offer_total / reference_total, 4) if reference_total else None
    return OfferResult(scenario, tuple(lines), cost.total_cost, offer_total, profit, margin, ratio)
