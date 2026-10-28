"""Kullanıcının ekrana yazdığı sayı ve tarihlerin ayrıştırılması.

Ekran girişinde Türkçe biçim esastır: ondalık ayraç virgül, binlik ayraç nokta.
"12.50" gibi belirsiz girişler tahmin edilmez, reddedilir.
(Dosya içe aktarmadaki sütun bazlı biçim tespiti Aşama 4'te ayrıca ele alınır.)
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

_GROUPED = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")
_PLAIN = re.compile(r"^\d+(,\d+)?$")
_DOT_DECIMAL = re.compile(r"^\d+\.\d+$")


def parse_user_decimal(text: str, *, allow_negative: bool = False) -> Decimal:
    s = text.strip().replace(" ", "").replace(" ", "")
    if not s:
        raise ValueError("Değer boş olamaz.")
    negative = s.startswith("-")
    if negative:
        if not allow_negative:
            raise ValueError("Negatif değer girilemez.")
        s = s[1:]
    if _GROUPED.match(s):
        s = s.replace(".", "").replace(",", ".")
    elif _PLAIN.match(s):
        s = s.replace(",", ".")
    elif _DOT_DECIMAL.match(s):
        raise ValueError(f"'{text.strip()}' belirsiz: ondalık ayraç olarak virgül kullanın (ör. 12,50).")
    else:
        raise ValueError(f"Geçersiz sayı: '{text.strip()}'")
    value = Decimal(s)
    return -value if negative else value


def parse_user_date(text: str) -> date:
    """'24.10.2026' ya da '2026-10-24' kabul edilir."""
    s = text.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Geçersiz tarih: '{s}'. GG.AA.YYYY biçiminde girin (ör. 24.10.2026).")


def format_date_tr(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return date.fromisoformat(iso[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return iso
