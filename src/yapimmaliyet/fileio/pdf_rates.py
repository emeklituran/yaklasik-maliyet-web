"""Birim fiyat kitaplarındaki ORAN pozları (Ölçü: %), ör. "25.300.2100 Bina içinde vidalı döşenmiş boru montaj
malzemesi bedeli; (Ölçü: %) % 30".

Bu kayıtlar fiyat değil orandır; fiyat tablosuna yazılmaz, ayrı saklanır. Okunan değer olduğu gibi alınır
(oranın hangi tutara uygulanacağı poz tarifinde yazar; program bunu yorumlamaz). Üst başlık satırı (oranı
olmayan "(Ölçü: %)" satırı) alt kalemlerin tanımına ön ek olarak eklenir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.fileio.pdf_text import open_pdf

_CODE_RE = re.compile(r"^(\d{2}\.\d{3}\.\d{4})\s+(.*)$")
_OLCU_PCT_RE = re.compile(r"\(?\s*Ölçü\s*:\s*%", re.IGNORECASE)
_RATE_TAIL_RE = re.compile(r"%\s*(\d{1,3}(?:,\d{1,2})?)\s*%?\)?\s*$")


@dataclass(frozen=True)
class RatePosition:
    code: str
    description: str
    rate: Decimal          # yüzde (30 → %30)
    page: int


def _rows(page) -> list[str]:
    rows: dict[int, list] = {}
    for w in page.get_text("words"):
        rows.setdefault(round((w[1] + w[3]) / 2 / 3), []).append(w)
    return [" ".join(x[4] for x in sorted(rows[k], key=lambda t: t[0])) for k in sorted(rows)]


def _clean(text: str) -> str:
    text = re.sub(r"\(?\s*Ölçü\s*:?\s*$", "", re.sub(r"\s+", " ", text), flags=re.IGNORECASE)
    return text.strip(" ;:,(")


def read_rate_positions(path: Path, pages: list[int] | None = None) -> list[RatePosition]:
    doc = open_pdf(path)
    out: list[RatePosition] = []
    seen: set[str] = set()
    try:
        numbers = pages or list(range(1, doc.page_count + 1))
        for no in numbers:
            page = doc[no - 1]
            text = page.get_text()
            if "%" not in text or "lçü" not in text:
                continue
            heading: tuple[str, str] | None = None   # (kod öneki, başlık tanımı)
            rows = _rows(page)
            for i, row in enumerate(rows):
                m = _CODE_RE.match(row)
                if not m:
                    continue
                body = m.group(2)
                nxt = rows[i + 1] if i + 1 < len(rows) and not _CODE_RE.match(rows[i + 1]) else ""
                if not _OLCU_PCT_RE.search(body) and not _OLCU_PCT_RE.search(nxt):
                    continue
                code = m.group(1)
                tail = _RATE_TAIL_RE.search(body)
                olcu = _OLCU_PCT_RE.search(body)
                if tail is None or (olcu is not None and tail.start() <= olcu.start()):
                    heading = (code[:9], _clean(_OLCU_PCT_RE.split(body)[0]))   # oranı olmayan başlık satırı
                    continue
                desc = body[:tail.start()]
                if olcu is None:              # tanımın devamı ve "(Ölçü: %)" bir alt satırda
                    desc += " " + _OLCU_PCT_RE.split(nxt)[0]
                desc = _clean(_OLCU_PCT_RE.split(desc)[0])
                rate = Decimal(tail.group(1).replace(",", "."))
                if not Decimal(0) < rate <= Decimal(100) or code in seen:
                    continue
                if heading and code.startswith(heading[0]) and code != heading[0]:
                    desc = f"{heading[1]}: {desc}"
                seen.add(code)
                out.append(RatePosition(code, desc, rate, no))
    finally:
        doc.close()
    return out
