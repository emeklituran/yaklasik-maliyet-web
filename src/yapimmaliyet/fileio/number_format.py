"""Dosyalardaki sayı biçiminin sütun bazında tespiti ve ayrıştırılması.

Tek bir değerden biçim tahmin edilmez ("1.234" hem 1234 hem 1,234 olabilir);
sütundaki bütün değerlere bakılır. Çelişki ya da belirsizlik kullanıcıya bildirilir.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class NumberFormat(StrEnum):
    TR = "TR"  # 1.234,56
    EN = "EN"  # 1,234.56

    @property
    def label(self) -> str:
        return "Türkçe (1.234,56)" if self is NumberFormat.TR else "İngilizce (1,234.56)"


_TR = re.compile(r"^(\d{1,3}(\.\d{3})+(,\d+)?|\d+(,\d+)?)$")
_EN = re.compile(r"^(\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?)$")
_STRIP = re.compile(r"(?i)\s| |tl|₺")


def _clean(text: str) -> tuple[bool, str]:
    s = _STRIP.sub("", text)
    negative = s.startswith("-")
    return negative, s[1:] if negative else s


def classify(text: str) -> str:
    """'PLAIN', 'TR', 'EN', 'AMBIGUOUS' ya da 'INVALID'."""
    _, s = _clean(text)
    if not s:
        return "INVALID"
    if s.isdigit():
        return "PLAIN"
    tr, en = bool(_TR.match(s)), bool(_EN.match(s))
    if tr and en:
        return "AMBIGUOUS"
    return "TR" if tr else "EN" if en else "INVALID"


@dataclass(frozen=True)
class FormatGuess:
    fmt: NumberFormat | None
    ambiguous: bool
    conflict: bool
    tr_votes: int
    en_votes: int


def detect_format(values: Iterable[str]) -> FormatGuess:
    tr = en = amb = 0
    for v in values:
        c = classify(v)
        tr += c == "TR"
        en += c == "EN"
        amb += c == "AMBIGUOUS"
    fmt = NumberFormat.TR if tr > en else NumberFormat.EN if en > tr else None
    return FormatGuess(fmt, ambiguous=amb > 0 and fmt is None, conflict=tr > 0 and en > 0,
                       tr_votes=tr, en_votes=en)


def parse_number(text: str, fmt: NumberFormat) -> Decimal:
    negative, s = _clean(text)
    if not s:
        raise ValueError("Sayı boş.")
    pattern = _TR if fmt is NumberFormat.TR else _EN
    if not pattern.match(s):
        raise ValueError(f"'{text.strip()}' değeri {fmt.label} sayı biçimine uymuyor.")
    s = s.replace(".", "").replace(",", ".") if fmt is NumberFormat.TR else s.replace(",", "")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"Geçersiz sayı: '{text.strip()}'") from exc
    return -value if negative else value
