"""Kütüphane karşılaştırmaları: iki fiyat kitabı (sürüm farkı) ve iki birim fiyat analizi (girdi farkı).

Yalnızca okur. Resmî analiz (analiz kitabı) ve özel poz analiz sürümü aynı karşılaştırma modeline dönüştürülür.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.repositories import official_analyses, price_books
from yapimmaliyet.domain import analysis
from yapimmaliyet.domain.analysis_compare import AnalysisComparison, Component, compare
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.text_tr import normalize_search
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.book_update_service import Change, DiffRow, MissingRow, UpdatePreview

TYPE_LABELS = {"MALZEME": "Malzeme", "ISCILIK": "İşçilik", "MAKINE": "Makine", "NAKLIYE": "Nakliye", "DIGER": "Diğer"}


@dataclass(frozen=True)
class AnalysisSide:
    label: str                 # "ÇŞB Mekanik Analiz 2026 — 25.100.1010 Lavabo"
    components: list[Component]
    total: Decimal | None      # analiz birim fiyatı (resmî ya da hesaplanan)
    source: str                # "Resmî analiz (s.12)" / "Özel poz sürüm 2 — kullanıcı analizi"


def compare_books(conn: sqlite3.Connection, old_id: int, new_id: int) -> UpdatePreview:
    """Eski kitaba göre yeni kitabın farkı (iki kitap da değişmez)."""
    if old_id == new_id:
        raise ValidationError("Karşılaştırma için iki farklı kitap seçin.")
    for b in (old_id, new_id):
        if price_books.get(conn, b) is None:
            raise NotFoundError("Fiyat kitabı bulunamadı.")
    old, new = price_books.prices_by_code(conn, old_id), price_books.prices_by_code(conn, new_id)
    rows = []
    for key, n in sorted(new.items()):
        price = from_db_text(n["unit_price"])
        o = old.get(key)
        if o is None:
            rows.append(DiffRow(n["code"], key, n["description"], n["unit"], price, Change.YENI, "yeni kitap"))
            continue
        old_price = from_db_text(o["unit_price"])
        change = Change.BIRIM if n["unit_norm"] != o["unit_norm"] else \
            (Change.AYNI if price == old_price else Change.FIYAT)
        om = from_db_text(o["montage_price"]) if o["montage_price"] is not None else None
        nm = from_db_text(n["montage_price"]) if n["montage_price"] is not None else None
        rows.append(DiffRow(n["code"], key, n["description"], n["unit"], price, change, "yeni kitap", old_price,
                            o["unit"], o["description"],
                            normalize_search(n["description"]) != normalize_search(o["description"]), om != nm))
    missing = [MissingRow(o["code"], o["description"], o["unit"], from_db_text(o["unit_price"]))
               for k, o in sorted(old.items()) if k not in new]
    return UpdatePreview(old_id, (0, 0), rows, missing)


def official_side(conn: sqlite3.Connection, analysis_id: int) -> AnalysisSide:
    a = official_analyses.get_analysis(conn, analysis_id)
    if a is None:
        raise NotFoundError("Analiz bulunamadı.")
    book = official_analyses.get_book(conn, a.book_id)
    comps = [Component(i.code, i.description, i.section, i.unit, i.quantity, i.unit_price, i.amount) for i in a.items]
    label = f"{book.title} {book.year} {book.edition}".strip() + f" — {a.code} {a.name[:60]}"
    return AnalysisSide(label, comps, a.unit_price, f"Resmî analiz{f' (s.{a.page})' if a.page else ''}")


def custom_side(conn: sqlite3.Connection, version_id: int) -> AnalysisSide:
    from yapimmaliyet.db.repositories import analyses

    spec = analyses.get_spec(conn, version_id)
    if spec is None:
        raise ValidationError("Seçilen özel poz sürümünün analizi yok (doğrudan fiyatlı).")
    row = conn.execute("SELECT cp.code, cp.name, v.version_no FROM custom_position_versions v"
                       " JOIN custom_positions cp ON cp.id = v.custom_position_id WHERE v.id = ?", (version_id,)).fetchone()
    res = analysis.compute(spec)
    comps = [Component(None, ln.item.description, TYPE_LABELS.get(ln.item.item_type.value, "Diğer"), ln.item.unit,
                       ln.effective_quantity, ln.item.unit_price, ln.amount) for ln in res.lines]
    return AnalysisSide(f"Özel poz {row['code']} {row['name'][:50]} (sürüm {row['version_no']})", comps,
                        res.unit_price, "Kullanıcı analizi (hesaplanan)")


def compare_sides(a: AnalysisSide, b: AnalysisSide) -> AnalysisComparison:
    cmp = compare(a.components, b.components, a.total, b.total)
    cmp.notes.append(f"1. analiz: {a.label} — {a.source}. 2. analiz: {b.label} — {b.source}.")
    cmp.notes.append("Analizler birebir eşdeğer olmayabilir; farklı girdiler eşleştirilmez, ayrıca listelenir.")
    return cmp


def find_official(conn: sqlite3.Connection, code: str, limit: int = 50) -> list[tuple[int, str]]:
    """Kod (ya da kod başlangıcı) ile resmî analiz araması: [(analiz kimliği, etiket)]."""
    rows = conn.execute(
        "SELECT oa.id, oa.code, oa.name, ab.title, ab.year, ab.edition FROM official_analyses oa"
        " JOIN analysis_books ab ON ab.id = oa.analysis_book_id WHERE oa.code LIKE ? ORDER BY oa.code, ab.year LIMIT ?",
        (code.strip() + "%", limit))
    return [(r["id"], f"{r['code']} — {r['name'][:70]} ({r['title']} {r['year']} {r['edition']})".strip())
            for r in rows]
