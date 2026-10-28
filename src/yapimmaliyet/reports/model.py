"""Biçimden bağımsız rapor modeli."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.money import format_price_tr, format_tr


class ColKind(StrEnum):
    TEXT = "TEXT"
    MONEY = "MONEY"      # 2 basamak
    PRICE = "PRICE"      # 2–4 basamak (kaynak hassasiyeti)
    QTY = "QTY"          # 3 basamak
    PERCENT = "PERCENT"  # kesir → %
    INT = "INT"


@dataclass(frozen=True)
class Col:
    title: str
    kind: ColKind = ColKind.TEXT
    width: float = 1.0   # göreli genişlik


Value = str | Decimal | int | None


@dataclass
class Table:
    columns: list[Col]
    rows: list[list[Value]]
    total_row: list[Value] | None = None


@dataclass
class Section:
    title: str
    table: Table | None = None
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class ReportDoc:
    title: str
    meta: list[tuple[str, str]]
    sections: list[Section]
    notes: list[str] = field(default_factory=list)
    draft: bool = False
    landscape: bool = True


def fmt(value: Value, kind: ColKind) -> str:
    if value is None or value == "":
        return ""
    if kind is ColKind.TEXT:
        return str(value)
    if kind is ColKind.INT:
        return str(value)
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if kind is ColKind.MONEY:
        return format_tr(d, 2)
    if kind is ColKind.PRICE:
        return format_price_tr(d)
    if kind is ColKind.QTY:
        return format_tr(d, 3)
    return "%" + format_tr(d * 100, 2)


def dec(text: str | None) -> Decimal | None:
    return None if text in (None, "") else Decimal(text)
