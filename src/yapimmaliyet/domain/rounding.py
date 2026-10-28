"""Yuvarlama politikası.

Varsayılan (Aşama 1 §7.5):
- Satır tutarı = round(miktar × birim fiyat, 2), ROUND_HALF_UP.
- Toplam = yuvarlanmış satır tutarlarının toplamı (rapordaki satırlar toplamla tutarlı olur).
- Ara adımlarda yuvarlama yapılmaz.
Politika hesap profili parametresidir; burada sabit kod değil, değiştirilebilir bir değerdir.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

from yapimmaliyet.domain.money import CALC_CONTEXT, to_decimal

SUPPORTED_MODES = (ROUND_HALF_UP, ROUND_HALF_EVEN)


@dataclass(frozen=True)
class RoundingPolicy:
    quantity_places: int = 3
    price_places: int = 2
    amount_places: int = 2
    analysis_places: int = 6
    mode: str = ROUND_HALF_UP

    def __post_init__(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise ValueError(f"Desteklenmeyen yuvarlama yöntemi: {self.mode}")
        for name in ("quantity_places", "price_places", "amount_places", "analysis_places"):
            if not 0 <= getattr(self, name) <= 8:
                raise ValueError(f"{name} 0 ile 8 arasında olmalıdır.")


DEFAULT_POLICY = RoundingPolicy()


def quantize(value: Decimal | int | str, places: int, mode: str = ROUND_HALF_UP) -> Decimal:
    q = to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=mode, context=CALC_CONTEXT)
    return abs(q) if q == 0 else q


def line_amount(quantity: Decimal | str, unit_price: Decimal | str,
                policy: RoundingPolicy = DEFAULT_POLICY) -> Decimal:
    product = CALC_CONTEXT.multiply(to_decimal(quantity), to_decimal(unit_price))
    return quantize(product, policy.amount_places, policy.mode)


def total(amounts: Iterable[Decimal], policy: RoundingPolicy = DEFAULT_POLICY) -> Decimal:
    """Satır tutarları zaten yuvarlanmış olmalıdır; toplam yeniden yuvarlanmaz, yalnızca ölçeklenir."""
    result = Decimal(0)
    for a in amounts:
        result = CALC_CONTEXT.add(result, to_decimal(a))
    return quantize(result, policy.amount_places, policy.mode)
