"""Hassas ondalık değerler.

Kurallar:
- Para, miktar ve oranlar her zaman `Decimal`'dir; `float` kabul edilmez.
- Veritabanında kanonik metin olarak saklanır ("1234.5600"); bilimsel gösterim yoktur.
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)

CALC_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP,
                       traps=[InvalidOperation, DivisionByZero, Overflow])


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Değeri `Decimal`'e çevirir. `float` ve `bool` reddedilir (sessiz hassasiyet kaybını önler)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("Ondalık değerler float olarak verilemez; str ya da Decimal kullanın.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("Boş değer sayıya çevrilemez.")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"Geçersiz sayı: {value!r}") from exc
    else:
        raise TypeError(f"Desteklenmeyen tür: {type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"Sonlu olmayan sayı kabul edilmez: {value!r}")
    return result


def to_db_text(value: Decimal | int | str, places: int) -> str:
    """Sabit basamaklı kanonik metin: to_db_text('3.1', 4) → '3.1000'."""
    d = to_decimal(value)
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP, context=CALC_CONTEXT)
    if q == 0:
        q = abs(q)  # "-0.00" yazılmasın
    return format(q, "f")


def from_db_text(text: str | None) -> Decimal | None:
    if text is None:
        return None
    return to_decimal(text)


def decimal_places(value: Decimal) -> int:
    exp = to_decimal(value).normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def format_price_tr(value: Decimal | None) -> str:
    """Fiyat gösterimi: en az 2, gerekiyorsa 4 basamak (kaynaktaki hassasiyet gizlenmez)."""
    if value is None:
        return ""
    return format_tr(value, 2 if decimal_places(value) <= 2 else 4)


def format_tr(value: Decimal, places: int = 2) -> str:
    """Türkçe sayı gösterimi: Decimal('1234567.5') → '1.234.567,50'."""
    q = to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP,
                                   context=CALC_CONTEXT)
    if q == 0:
        q = abs(q)
    text = f"{q:,.{places}f}"  # 1,234,567.50
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
