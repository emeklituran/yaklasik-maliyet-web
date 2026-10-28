"""Poz eşleştirme puanlaması (veritabanından bağımsız).

Benzer açıklama hiçbir zaman otomatik olarak aynı poz sayılmaz; yalnızca aday olarak önerilir.
Sıralama: tam kod eşleşmesi → ana poz (ekli kodun eksiz hâli) → kaynak önceliği → birim uyumu →
açıklama benzerliği.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit

# Benzerlik hesabında anlam taşımayan sık kelimeler
STOPWORDS = frozenset({"ve", "ile", "icin", "her", "turlu", "dahil", "yapilmasi", "yapilmasinin", "olarak",
                       "bir", "da", "de", "ait", "gore", "veya", "en", "az", "cm", "mm"})

OFFICIAL = "RESMI"
CUSTOM = "OZEL"

# Keşiflerde ana poza eklenen son ekler: "-D" (demontaj), "/A", "/B" (montaj malzemesi dahil vb.).
# Ekli kalemin fiyatı ana pozun fiyatıyla aynı değildir; ana poz yalnızca aday olarak gösterilir.
_SUFFIX_RE = re.compile(r"^(.*\d)(-D|/[A-Z])$")


def split_suffix(code: str | None) -> tuple[str, str] | None:
    """"25.300.1101/A" → ("25.300.1101", "/A"); eki olmayan kodda None."""
    m = _SUFFIX_RE.match(normalize_code(code)) if code else None
    return (m.group(1), m.group(2)) if m else None

def tokens(text: str | None) -> set[str]:
    return {t for t in normalize_search(text or "").split() if len(t) >= 2 and t not in STOPWORDS}


def similarity(a: str | None, b: str | None) -> float:
    """0–1 arası: kelime kümesi örtüşmesi (%60) ve karakter dizisi benzerliği (%40)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return round(0.6 * jaccard + 0.4 * seq, 4)


@dataclass(frozen=True)
class CandidateSource:
    kind: str                # RESMI | OZEL
    ref_id: int              # position_price_id ya da custom_version_id
    code: str
    description: str
    unit: str
    unit_price: Decimal | None
    source_label: str        # "ÇŞB — Birim Fiyatlar 2026" / "Özel poz (s.2)"
    book_id: int | None = None


@dataclass(frozen=True)
class Candidate:
    source: CandidateSource
    exact_code: bool
    priority: int            # kaynak öncelik sırası (küçük olan önce); listede yoksa 999
    unit_ok: bool | None     # None: keşifte birim yok
    score: float
    base_suffix: str | None = None  # ekli kodun ana pozu ise ek ("-D", "/A"…)

    @property
    def reason(self) -> str:
        if self.exact_code:
            parts = ["Tam kod eşleşmesi"]
        elif self.base_suffix:
            parts = [f"ANA POZ (keşifteki kod '{self.base_suffix}' ekli) — fiyat farklı olabilir, kontrol edin"]
        else:
            parts = [f"Açıklama benzerliği %{self.score * 100:.0f}"]
        if self.unit_ok is False:
            parts.append("BİRİM UYUŞMUYOR")
        if self.source.kind == CUSTOM:
            parts.append("özel poz")
        return " · ".join(parts)


def units_match(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    return normalize_unit(a) == normalize_unit(b)


def rank(code: str | None, description: str | None, unit: str | None,
         sources: list[CandidateSource], priority: list[int], limit: int = 10) -> list[Candidate]:
    code_key = normalize_code(code) if code else None
    base = split_suffix(code)
    order = {book_id: i for i, book_id in enumerate(priority)}
    out = []
    for s in sources:
        key = normalize_code(s.code)
        exact = bool(code_key) and key == code_key
        suffix = base[1] if base and not exact and key == base[0] else None
        score = 1.0 if exact else similarity(description, s.description)
        if not exact and not suffix and score < 0.25:
            continue
        prio = order.get(s.book_id, 999) if s.kind == OFFICIAL else 500
        out.append(Candidate(s, exact, prio, units_match(unit, s.unit), score, suffix))
    out.sort(key=lambda c: (not c.exact_code, c.base_suffix is None,
                            c.priority if c.exact_code or c.base_suffix else 0, c.unit_ok is False,
                            -c.score, c.priority))
    return out[:limit]
