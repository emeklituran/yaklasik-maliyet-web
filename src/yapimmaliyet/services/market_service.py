"""Açılış ekranı için piyasa bilgisi: döviz, altın ve borsa endeksi (yalnız bilgi amaçlı).

Kaynak sırası: Truncgil Finans (döviz, gram / çeyrek altın, BIST 100; günlük değişimle) → TCMB günlük bülteni
(döviz satış) → son başarılı alımın önbelleği. Ağ hatası programın açılışını hiçbir zaman engellemez; değerler
Decimal'e çevrilir, pozitif ve makul olmayan değerler atılır. Bu veriler hesaplarda kullanılmaz; keşfe sabitlenen
resmî kur yine Döviz Kuru penceresinden TCMB'den alınır.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

TRUNCGIL_URL = "https://finans.truncgil.com/v4/today.json"
TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
TIMEOUT_S = 4
MAX_BYTES = 1_500_000
# (anahtar, ad, Truncgil anahtarı, TCMB döviz kodu)
ITEMS = (("USD", "ABD Doları", "USD", "USD"), ("EUR", "Euro", "EUR", "EUR"), ("GBP", "İngiliz Sterlini", "GBP", "GBP"),
         ("GRAM", "Gram Altın", "GRA", None), ("CEYREK", "Çeyrek Altın", "CEYREKALTIN", None),
         ("BIST", "BIST 100", "XU100", None))


@dataclass
class MarketItem:
    key: str
    name: str
    value: str             # Decimal metni (JSON önbelleği için)
    change: str | None = None   # günlük değişim yüzdesi

    @property
    def amount(self) -> Decimal:
        return Decimal(self.value)


@dataclass
class MarketData:
    source: str
    at: str                 # kaynağın güncelleme zamanı (metin)
    items: list[MarketItem] = field(default_factory=list)
    cached: bool = False    # bağlantı yok; son alınan değerler


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "YapimMaliyet"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:   # noqa: S310 (sabit https adresi)
        return resp.read(MAX_BYTES)


def _dec(value) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and Decimal(0) < d < Decimal(10) ** 9 else None


def parse_truncgil(content: bytes) -> MarketData | None:
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict):
        return None
    items = []
    for key, name, src, _ in ITEMS:
        entry = raw.get(src)
        if not isinstance(entry, dict):
            continue
        value = _dec(entry.get("Selling"))
        if value is None:
            continue
        change = entry.get("Change")
        try:
            change_text = str(Decimal(str(change)).quantize(Decimal("0.01"))) if change is not None else None
        except (InvalidOperation, ValueError):
            change_text = None
        items.append(MarketItem(key, name, str(value), change_text))
    return MarketData("Truncgil Finans", str(raw.get("Update_Date", ""))[:16], items) if items else None


def parse_tcmb(content: bytes) -> MarketData | None:
    from yapimmaliyet.fileio import tcmb_xml

    bulletin = tcmb_xml.parse(content)
    wanted = {code: (key, name) for key, name, _, code in ITEMS if code}
    items = [MarketItem(wanted[r.currency][0], wanted[r.currency][1], str(r.per_unit))
             for r in bulletin.rates if r.currency in wanted and r.rate_type == "DOVIZ_SATIS"]
    return MarketData("TCMB döviz satış", bulletin.date, items) if items else None


def fetch(cache: Path | None = None, getter=_get) -> MarketData | None:
    """Sırayla kaynakları dener; başarılı sonucu önbelleğe yazar. Hiçbiri yoksa önbellek (`cached=True`) ya da None."""
    for url, parser in ((TRUNCGIL_URL, parse_truncgil), (TCMB_URL, parse_tcmb)):
        try:
            data = parser(getter(url))
        except Exception:   # ağ, biçim ya da ayrıştırma hatası: sıradaki kaynağa geçilir
            data = None
        if data is not None:
            if cache is not None:
                save_cache(cache, data)
            return data
    return load_cache(cache) if cache is not None else None


def save_cache(path: Path, data: MarketData) -> None:
    try:
        payload = {**asdict(data), "saved": datetime.now().isoformat(timespec="minutes")}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass   # salt okunur klasör: önbelleksiz devam


def load_cache(path: Path) -> MarketData | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = [MarketItem(i["key"], i["name"], str(_dec(i["value"])), i.get("change"))
                 for i in raw["items"] if _dec(i.get("value")) is not None]
        return MarketData(str(raw["source"]), str(raw.get("saved") or raw.get("at", "")), items, cached=True) if items else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
