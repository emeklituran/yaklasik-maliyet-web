"""TCMB gösterge niteliğindeki döviz kurları bülteninin (kurlar/today.xml) ayrıştırılması.

Yalnızca ayrıştırma yapar; ağ erişimi servis katmanındadır. DTD/varlık tanımı içeren içerik reddedilir
(dış varlık ve "billion laughs" saldırılarına karşı). Boş kur alanları atlanır; değer uydurulmaz.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
FIELDS = {"ForexBuying": "DOVIZ_ALIS", "ForexSelling": "DOVIZ_SATIS",
          "BanknoteBuying": "EFEKTIF_ALIS", "BanknoteSelling": "EFEKTIF_SATIS"}


def archive_url(day: str) -> str:
    """Geçmiş tarihli bülten adresi (ISO tarih → .../kurlar/YYYYMM/DDMMYYYY.xml)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"https://www.tcmb.gov.tr/kurlar/{d:%Y%m}/{d:%d%m%Y}.xml"


@dataclass(frozen=True)
class TcmbRate:
    currency: str
    name: str
    unit: int
    rate_type: str
    raw: Decimal          # bültendeki değer (birim başına)
    per_unit: Decimal     # 1 birim dövizin TL karşılığı (raw / unit)


@dataclass(frozen=True)
class TcmbBulletin:
    date: str             # ISO
    bulletin_no: str
    rates: tuple[TcmbRate, ...]


class TcmbFormatError(ValueError):
    pass


def parse(content: bytes) -> TcmbBulletin:
    head = content[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in content.upper():
        raise TcmbFormatError("Kur dosyası beklenmeyen tanımlar (DTD/ENTITY) içeriyor; işlenmedi.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise TcmbFormatError(f"Kur dosyası okunamadı: {exc}") from exc
    if root.tag != "Tarih_Date" or not root.get("Tarih"):
        raise TcmbFormatError("Beklenen TCMB kur bülteni biçimi bulunamadı (Tarih_Date).")
    try:
        day = datetime.strptime(root.get("Tarih"), "%d.%m.%Y").date().isoformat()
    except ValueError as exc:
        raise TcmbFormatError(f"Bülten tarihi okunamadı: {root.get('Tarih')}") from exc
    rates = []
    for cur in root.findall("Currency"):
        code = (cur.get("CurrencyCode") or cur.get("Kod") or "").strip().upper()
        if len(code) != 3 or code == "XDR":     # XDR (SDR) döviz değil, sepet birimi
            continue
        try:
            unit = int((cur.findtext("Unit") or "1").strip())
        except ValueError:
            continue
        name = (cur.findtext("Isim") or cur.findtext("CurrencyName") or code).strip()
        for tag, rate_type in FIELDS.items():
            text = (cur.findtext(tag) or "").strip()
            if not text:
                continue
            try:
                raw = Decimal(text)
            except InvalidOperation:
                continue
            if raw > 0 and unit >= 1:
                rates.append(TcmbRate(code, name, unit, rate_type, raw, raw / unit))
    if not rates:
        raise TcmbFormatError("Bültende okunabilir kur bulunamadı.")
    return TcmbBulletin(day, root.get("Bulten_No", ""), tuple(rates))
