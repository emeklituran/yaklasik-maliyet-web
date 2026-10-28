"""Okuyucuların ortak çıktısı: kaynak konumunu koruyan ham tablo."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

CellValue = str | Decimal | None


class SourceKind(StrEnum):
    EXCEL = "EXCEL"
    CSV = "CSV"
    PDF_METIN = "PDF_METIN"
    PDF_OCR = "PDF_OCR"

    @property
    def label(self) -> str:
        return {"EXCEL": "Excel", "CSV": "CSV", "PDF_METIN": "PDF (metin)", "PDF_OCR": "PDF (OCR)"}[self.value]


@dataclass
class RawRow:
    values: list[CellValue]
    ref: str  # kullanıcıya gösterilen kaynak konumu: "Sayfa1 satır 12", "s.3 sat.14"
    page: int | None = None
    line: int | None = None
    confidences: list[float | None] | None = None  # yalnızca OCR
    hidden: bool = False  # Excel'de gizlenmiş satır (varsayılan olarak aktarılmaz)
    merged: dict[int, CellValue] | None = None  # dikey birleştirilmiş hücrede üst hücrenin değeri (sütun → değer)

    def cell(self, col: int) -> CellValue:
        return self.values[col] if 0 <= col < len(self.values) else None

    def confidence(self, col: int) -> float | None:
        if self.confidences is None or not 0 <= col < len(self.confidences):
            return None
        return self.confidences[col]


@dataclass
class RawTable:
    name: str
    rows: list[RawRow]
    source_kind: SourceKind
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    hidden_columns: set[int] = field(default_factory=set)  # Excel'de gizlenmiş sütunlar (0'dan başlar)

    @property
    def width(self) -> int:
        return max((len(r.values) for r in self.rows), default=0)


def cell_text(value: CellValue) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return value
