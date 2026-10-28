"""Servislerin ortak girdi doğrulamaları (sistem sınırı)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from yapimmaliyet.domain.models import PRICE_DB_PLACES, QUANTITY_PLACES
from yapimmaliyet.domain.money import decimal_places, to_decimal
from yapimmaliyet.infra.errors import ValidationError


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None


def require(text: str | None, field: str) -> str:
    value = clean(text)
    if not value:
        raise ValidationError(f"{field} boş bırakılamaz.")
    return value


def validate_price(value: Decimal) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Geçersiz fiyat: {value}") from exc
    if d < 0:
        raise ValidationError("Birim fiyat negatif olamaz.")
    if decimal_places(d) > PRICE_DB_PLACES:
        raise ValidationError(f"Birim fiyat en fazla {PRICE_DB_PLACES} ondalık basamak içerebilir: {d}")
    return d


def validate_quantity(value: Decimal) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Geçersiz miktar: {value}") from exc
    if d < 0:
        raise ValidationError("Miktar negatif olamaz.")
    if decimal_places(d) > QUANTITY_PLACES:
        raise ValidationError(f"Miktar en fazla {QUANTITY_PLACES} ondalık basamak içerebilir.")
    return d


def validate_iso_date(value: str | None, field: str, *, required: bool = False) -> str | None:
    value = clean(value)
    if value is None:
        if required:
            raise ValidationError(f"{field} boş bırakılamaz.")
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValidationError(f"{field} geçerli bir tarih değil: {value}") from exc
