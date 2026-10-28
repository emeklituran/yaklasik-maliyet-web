"""Keşif eşleştirme incelemesi: her kalemin durumu tek bir kategoriye ayrılır ve gerekçesi yazılır.

Yalnızca okur; eşleştirme kararlarını değiştirmez, fiyat atamaz. Poz kodları olduğu gibi gösterilir; şüpheli kodlar
için düzeltme önerisi ayrı bir metinde verilir (kodun kendisi değiştirilmez).
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum

from yapimmaliyet.db.repositories import estimate_items, projects
from yapimmaliyet.db.repositories.estimate_items import EstimateItem
from yapimmaliyet.domain.matching import split_suffix
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.text_tr import normalize_code, tr_upper
from yapimmaliyet.services import suffix_pricing

OFFICIAL_CODE_RE = re.compile(r"^\d{2}\.\d{3}\.\d{4}")
PREFIX_RE = re.compile(r"^(.*?)(\d+)$")
STRONG, MULTI = 0.75, 0.6          # açıklama benzerliği eşikleri (yalnızca öneri; onay her zaman kullanıcıda)


class Category(StrEnum):
    ONAYLI = "ONAYLI"
    ONERI = "ONERI"
    ALTERNATIF = "ALTERNATIF"
    GUCLU = "GUCLU"
    COKLU = "COKLU"
    BIRIM = "BIRIM"
    EKLI_KOD = "EKLI_KOD"
    OZEL = "OZEL"
    MANUEL = "MANUEL"
    YOK = "YOK"
    ERTELENDI = "ERTELENDI"

    @property
    def label(self) -> str:
        return {"ONAYLI": "Onaylı eşleşme", "ONERI": "Kesin kod eşleşmesi — onay bekliyor",
                "ALTERNATIF": "Kaynak dayanaklı alternatif — onay gerekli",
                "GUCLU": "Güçlü öneri (açıklama) — onay gerekli", "COKLU": "Birden fazla olası eşleşme",
                "BIRIM": "Birim uyuşmazlığı", "EKLI_KOD": "Ekli kod — ana poz var, kaynakta kural yok",
                "OZEL": "Özel poz — resmî listede yok", "MANUEL": "Manuel inceleme gerekli",
                "YOK": "Kaynakta bulunamadı", "ERTELENDI": "Ertelendi"}[self.value]

    @property
    def needs_action(self) -> bool:
        return self is not Category.ONAYLI


@dataclass(frozen=True)
class ReviewRow:
    item: EstimateItem
    category: Category
    note: str
    duplicate_count: int = 1
    suggestion: str = ""                       # şüpheli kod için düzeltme önerisi (kod değiştirilmez)
    proposal: suffix_pricing.SuffixProposal | None = None

    @property
    def priced(self) -> bool:
        return self.item.user_price_override is not None or self.item.matched_price is not None


@dataclass(frozen=True)
class Review:
    rows: list[ReviewRow]

    def counts(self) -> dict[Category, int]:
        c = Counter(r.category for r in self.rows)
        return {k: c.get(k, 0) for k in Category}

    @property
    def duplicates(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.duplicate_count > 1]

    @property
    def unpriced(self) -> list[ReviewRow]:
        return [r for r in self.rows if not r.priced]

    @property
    def open_items(self) -> list[ReviewRow]:
        return [r for r in self.rows if r.category.needs_action]

    def priced_amount_check(self) -> Decimal:
        """Bağımsız kontrol: fiyatlı satırların miktar × birim fiyat toplamı (yuvarlamasız, bilgi amaçlı)."""
        total = Decimal(0)
        for r in self.rows:
            price = r.item.user_price_override if r.item.user_price_override is not None else r.item.matched_price
            if price is not None:
                total += r.item.quantity * price
        return total


def _custom_prefixes(items: list[EstimateItem]) -> set[str]:
    """Resmî desene uymayan ve keşifte en az 3 kez geçen kod önekleri (ör. 'ÖZEL MT-') özel poz sayılır."""
    c = Counter()
    for it in items:
        m = PREFIX_RE.match(tr_upper(it.code or ""))
        if m and not OFFICIAL_CODE_RE.match(it.code or ""):
            c[m.group(1)] += 1
    return {p for p, n in c.items() if n >= 3}


def _suspicious(code: str, prefixes: set[str], codes: set[str]) -> str:
    m = PREFIX_RE.match(tr_upper(code))
    if not m or m.group(1) in prefixes:
        return ""
    best = max(prefixes, key=lambda p: SequenceMatcher(None, m.group(1), p).ratio(), default=None)
    if best is None or SequenceMatcher(None, m.group(1), best).ratio() < 0.8:
        return ""
    fixed = best + m.group(2)
    exists = "keşifte bu kod ayrıca var" if normalize_code(fixed) in codes else "keşifte bu kod başka satırda yok"
    return f"Olası yazım hatası: “{code}” → “{fixed}” ({exists}). Orijinal kod değiştirilmedi."


def _classify(conn, it: EstimateItem, ctx: dict) -> tuple[Category, str, str, object]:
    if it.match_status == "ERTELENDI":
        return Category.ERTELENDI, "Kullanıcı sonraya bıraktı.", "", None
    if it.match_status in ("ONERILDI", "ONAYLANDI") and it.unit_mismatch and not it.unit_mismatch_ack:
        return Category.BIRIM, (f"Keşifte '{it.unit or '-'}', poz kaydında '{it.matched_unit or '-'}'. Dönüşüm "
                                "kuralı bilinmeden hesaba alınmaz."), "", None
    if it.match_status == "ONAYLANDI":
        return Category.ONAYLI, it.source_label or "", "", None
    if it.match_status == "ONERILDI":
        return Category.ONERI, f"Tam kod eşleşmesi: {it.source_label}", "", None
    code = it.code or ""
    m = PREFIX_RE.match(tr_upper(code))
    if code and not OFFICIAL_CODE_RE.match(code) and m and m.group(1) in ctx["prefixes"]:
        return Category.OZEL, "Resmî ÇŞB listesinde aranmaz; özel poz (doğrudan fiyat ya da analiz) olarak girin.", \
            "", None
    if code and not OFFICIAL_CODE_RE.match(code):
        hint = _suspicious(code, ctx["prefixes"], ctx["codes"])
        if hint:
            return Category.MANUEL, "Kod hiçbir desene uymuyor.", hint, None
    parts = split_suffix(code)
    if parts and parts[0] in ctx["library"]:
        proposal = suffix_pricing.propose(conn, it, ctx["priority"])
        if proposal is not None:
            return Category.ALTERNATIF, proposal.explanation, "", proposal
        if parts[1] == "-D":
            return Category.MANUEL, (f"'-D' ekli kod (demontaj olabilir): incelenen resmî kaynaklarda demontaj "
                                     f"fiyatı kuralı bulunamadı; ana poz {parts[0]} var. İdare belgesiyle "
                                     "fiyatlandırın."), "", None
        return Category.EKLI_KOD, (f"'{parts[1]}' ekli kod kitapta yok; ana poz {parts[0]} var. Ekli kodun fiyatı "
                                   "ana pozdan farklı olabilir."), "", None
    cands = ctx["matching"].candidates(it.id, limit=5) if (it.description or "").strip() else []
    good = [c for c in cands if c.score >= MULTI and not c.exact_code]
    if len(good) >= 2 and good[0].score - good[1].score < 0.1:
        return Category.COKLU, f"{len(good)} poz açıklamaca benzer (en yüksek %{good[0].score * 100:.0f}); " \
                               "seçim kullanıcıda.", "", None
    if good and good[0].score >= STRONG:
        return Category.GUCLU, (f"{good[0].source.code} açıklamaca %{good[0].score * 100:.0f} benzer "
                                f"({good[0].source.source_label}); kod farklı, onay gerekli."), "", None
    if not code:
        return Category.YOK, "Poz numarası yok ve yeterince benzer açıklama bulunamadı.", "", None
    return Category.YOK, "Bu kod seçili fiyat kitaplarında ve özel pozlarda bulunamadı.", "", None


def multi_book_prices(conn: sqlite3.Connection, codes: set[str],
                      book_ids: list[int]) -> dict[str, list[tuple[int, Decimal]]]:
    """Seçili kitapların birden fazlasında FARKLI fiyatla bulunan keşif kodları: kod → [(kitap, fiyat)] (öncelik
    sırasıyla). Öncelik sırasının sonucu değiştirdiği kalemleri kullanıcıya göstermek içindir."""
    if not codes or len(book_ids) < 2:
        return {}
    marks = ",".join("?" * len(book_ids))
    found: dict[str, dict[int, Decimal]] = {}
    for r in conn.execute(f"SELECT p.code_norm, pp.price_book_id, pp.unit_price FROM position_prices pp"
                          f" JOIN positions p ON p.id = pp.position_id WHERE pp.price_book_id IN ({marks})", book_ids):
        if r[0] in codes:
            found.setdefault(r[0], {})[r[1]] = from_db_text(r[2])
    order = {b: i for i, b in enumerate(book_ids)}
    return {code: sorted(prices.items(), key=lambda kv: order[kv[0]]) for code, prices in found.items()
            if len(set(prices.values())) > 1}


def review(conn: sqlite3.Connection, estimate_id: int) -> Review:
    from yapimmaliyet.services.matching_service import MatchingService

    items = estimate_items.list_items(conn, estimate_id)
    est = projects.get_estimate(conn, estimate_id)
    ctx = {"library": {r[0] for r in conn.execute("SELECT code_norm FROM positions")},
           "prefixes": _custom_prefixes(items), "codes": {normalize_code(i.code) for i in items if i.code},
           "priority": list(est.source_priority) if est else [], "matching": MatchingService(conn)}
    dup = Counter(normalize_code(i.code) for i in items if i.code)
    rows = []
    for it in items:
        cat, note, hint, proposal = _classify(conn, it, ctx)
        rows.append(ReviewRow(it, cat, note, dup[normalize_code(it.code)] if it.code else 1, hint, proposal))
    return Review(rows)
