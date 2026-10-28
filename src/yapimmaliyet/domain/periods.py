"""Fiyat dönemi, geçerlilik tarihi, fiyat türü ve doğrulama durumu: etiketler ve belgeden dönem tespiti.

Yayın tarihi, geçerlilik başlangıcı ve içe aktarma tarihi ayrı kavramlardır; tespit edilemeyen bilgi uydurulmaz,
None döner ve arayüzde "Belirtilmemiş" gösterilir. Tespit yalnız öneridir; kullanıcı içe aktarmada onaylar ya da düzeltir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNSPECIFIED = "Belirtilmemiş"

PRICE_KINDS = {
    "MALZEME": "Malzeme",
    "MONTAJ": "Montaj",
    "MALZEME_MONTAJ": "Malzeme + montaj",
    "DEMONTAJ": "Demontaj",
    "DEMONTAJDAN_MONTAJ": "Demontajdan montaj",
    "RAYIC": "Rayiç",
    "ANALIZ": "Analiz",
    "DIGER": "Diğer",
    "BELIRTILMEMIS": UNSPECIFIED,
}

VERIFICATION = {
    "DOGRULANDI": "Doğrulandı",
    "KULLANICI": "Kullanıcı aktarımı",
    "KONTROL": "Kontrol gerekiyor",
    "ADAY": "Aday (doğrulanmamış)",
}
UNVERIFIED = ("ADAY", "KONTROL")   # bu durumdaki kitaplar aktifleştirilemez, hesaplarda kullanılmaz

MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
_MONTH_KEYS = ("ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim", "kasim",
               "aralik")
_ISO = re.compile(r"^[12]\d{3}-[01]\d-[0-3]\d$")


def _fold(text: str) -> str:
    return text.lower().translate(str.maketrans("çğıöşüâîû", "cgiosuaiu")).replace("i̇", "i")


def kind_label(kind: str | None) -> str:
    return PRICE_KINDS.get(kind or "BELIRTILMEMIS", kind or UNSPECIFIED)


def verification_label(value: str | None) -> str:
    return VERIFICATION.get(value or "", value or UNSPECIFIED)


def date_tr(iso: str | None) -> str:
    """'2026-01-01' → '01.01.2026'; boşsa 'Belirtilmemiş'."""
    if not iso or not _ISO.match(iso):
        return iso or UNSPECIFIED
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def text_or_unspecified(value) -> str:
    return str(value) if value not in (None, "") else UNSPECIFIED


def period_from_catalog(value: str | None) -> str | None:
    """Katalog dönem kodu ('2026-01', '2026', '2026-1.donem') → okunur etiket ('2026 Ocak', '2026', '2026 1. Dönem')."""
    if not value:
        return None
    m = re.fullmatch(r"([12]\d{3})-(\d{2})", value)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)} {MONTHS[int(m.group(2)) - 1]}"
    m = re.fullmatch(r"([12]\d{3})-(\d)\.donem", value)
    if m:
        return f"{m.group(1)} {m.group(2)}. Dönem"
    return value


@dataclass(frozen=True)
class PeriodGuess:
    year: int | None
    period: str | None        # "2026 Ocak", "2026/2", "2026 1. Dönem"
    valid_from: str | None    # ISO; yalnız belgede açık tarih varsa
    evidence: str             # tespitin dayandığı metin parçası (kullanıcıya gösterilir)

    @property
    def found(self) -> bool:
        return self.year is not None


def detect_period(text: str) -> PeriodGuess:
    """Belge metninden yıl, dönem ve (açıkça yazılıysa) geçerlilik başlangıcı önerisi.

    Öncelik: 'YYYY yılı ... ay' / 'Ay YYYY' (aylık dönem) → 'YYYY/N' ya da 'N. dönem' → tek yıl. Tarih yalnız 'geçerli',
    'yürürlük' ya da 'itibaren' sözcüğü yakınındaki gg.aa.yyyy biçiminden alınır; başka tarih uydurulmaz."""
    folded = _fold(text or "")
    valid_from, evidence = None, ""
    m = re.search(r"(\d{2})[./](\d{2})[./]([12]\d{3})[^\n]{0,40}?(itibaren|gecerli|yururl)", folded) or \
        re.search(r"(gecerli|yururl|itibaren)[^\n]{0,40}?(\d{2})[./](\d{2})[./]([12]\d{3})", folded)
    if m:
        g = m.groups()
        d, mo, y = (g[0], g[1], g[2]) if g[0].isdigit() else (g[1], g[2], g[3])
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            valid_from, evidence = f"{y}-{mo}-{d}", m.group(0)
    months = "|".join(_MONTH_KEYS)
    m = re.search(rf"([12]\d{{3}})\s*(?:yili)?\s*({months})\b", folded) or re.search(rf"\b({months})\s*([12]\d{{3}})",
                                                                                         folded)
    if m:
        a, b = m.groups()
        year, month = (int(a), b) if a.isdigit() else (int(b), a)
        return PeriodGuess(year, f"{year} {MONTHS[_MONTH_KEYS.index(month)]}", valid_from, evidence or m.group(0))
    m = re.search(r"([12]\d{3})\s*/\s*([1-9]|1[0-2])\b(?!\s*[./-]\d)", folded)
    if m:
        return PeriodGuess(int(m.group(1)), f"{m.group(1)}/{m.group(2)}", valid_from, evidence or m.group(0))
    m = re.search(r"([12]\d{3})[^\n]{0,20}?\b([1-4])\s*\.\s*donem", folded)
    if m:
        return PeriodGuess(int(m.group(1)), f"{m.group(1)} {m.group(2)}. Dönem", valid_from, evidence or m.group(0))
    m = re.search(r"\b(20[2-9]\d)\s*yili", folded) or re.search(r"\b(20[2-9]\d)\b", folded)
    if m:
        year = int(m.group(1))
        if valid_from:
            return PeriodGuess(year, None, valid_from, evidence)
        return PeriodGuess(year, None, None, m.group(0))
    if valid_from:
        return PeriodGuess(int(valid_from[:4]), None, valid_from, evidence)
    return PeriodGuess(None, None, None, "")
