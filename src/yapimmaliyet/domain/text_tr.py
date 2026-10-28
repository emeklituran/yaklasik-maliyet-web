"""Türkçe metin normalizasyonu (arama, poz kodu ve birim karşılaştırması için).

Python'un str.lower()/upper() işlevleri Türkçe İ/ı harflerini yanlış çevirir
("İ".lower() → "i̇"). Bu modül bütün karşılaştırmalar için tek doğru kaynaktır.
"""

from __future__ import annotations

import re
import unicodedata

_ASCII_FOLD = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
    "â": "a", "î": "i", "û": "u",
})
_SUPERSCRIPTS = str.maketrans({"²": "2", "³": "3", "¹": "1"})
_DASHES = str.maketrans({"–": "-", "—": "-", "‐": "-", "‒": "-"})
_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^0-9a-z]+")


def tr_lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def tr_upper(text: str) -> str:
    return text.replace("i", "İ").replace("ı", "I").upper()


def normalize_search(text: str) -> str:
    """Arama anahtarı: küçük harf, Türkçe harfler ASCII'ye, noktalama boşluğa.

    'ŞAP', 'şap' ve 'sap' aynı anahtarı üretir.
    """
    s = tr_lower(text).translate(_SUPERSCRIPTS).translate(_ASCII_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _NON_WORD.sub(" ", s)
    return _WS.sub(" ", s).strip()


def normalize_code(code: str) -> str:
    """Poz kodu karşılaştırma anahtarı.

    Büyük harf, boşluklar silinir, farklı tire karakterleri birleştirilir, sondaki
    noktalar atılır. Nokta, eğik çizgi gibi ayraçlar korunur; çünkü '15.150' ile
    '15150' farklı pozlar olabilir.
    """
    s = unicodedata.normalize("NFC", code).translate(_DASHES)
    s = _WS.sub("", tr_upper(s))
    return s.rstrip(".")


_UNIT_ALIASES = {
    "m2": "m2", "metrekare": "m2",
    "m3": "m3", "metrekup": "m3",
    "m": "m", "mt": "m", "metre": "m", "mtul": "m",
    "kg": "kg", "ton": "ton", "t": "ton",
    "ad": "adet", "adet": "adet", "ade": "adet",
    "sa": "saat", "saat": "saat",
    "lt": "lt", "l": "lt", "litre": "lt",
    "kum": "kum", "kumulatif": "kum",
}


def normalize_unit(unit: str) -> str:
    """Birim karşılaştırma anahtarı: 'M²', 'm2', 'm 2' → 'm2'. Tanınmayan birim olduğu gibi (normalize) döner."""
    key = normalize_search(unit).replace(" ", "")
    return _UNIT_ALIASES.get(key, key)
