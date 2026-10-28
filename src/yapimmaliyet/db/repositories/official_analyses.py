"""Resmî analiz kitapları, analizler ve bileşen satırları (0007).

Bu tablolar birim fiyat kitaplarından bağımsızdır. Fiyat kütüphanesi (positions, position_prices,
price_books) burada yalnızca karşılaştırma için OKUNUR; hiçbir fonksiyon oraya yazmaz.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text


@dataclass(frozen=True)
class AnalysisBookRow:
    id: int
    authority_id: int
    authority_name: str
    title: str
    year: int
    edition: str
    source_file_id: int | None
    source_name: str | None
    created_at: str
    analysis_count: int
    consistent_count: int
    period: str | None = None       # analiz sayfalarındaki tarihten (ör. "2026 Ocak"); yoksa None
    valid_from: str | None = None


@dataclass(frozen=True)
class AnalysisItemRow:
    id: int
    line_no: int
    section: str | None
    code: str | None
    description: str
    unit: str | None
    quantity: Decimal | None
    quantity_text: str | None
    unit_price: Decimal | None
    amount: Decimal | None
    amount_column: str | None


@dataclass(frozen=True)
class AnalysisRow:
    id: int
    book_id: int
    code: str
    code_norm: str
    name: str
    unit: str | None
    analysis_date: str | None
    group_label: str | None
    subtotal: Decimal | None
    material_total: Decimal | None
    labour_total: Decimal | None
    overhead_rate: Decimal | None
    overhead_amount: Decimal | None
    unit_price: Decimal | None
    montage_price: Decimal | None
    notes: str | None
    page: int | None
    check_ok: bool
    warnings: list[str] = field(default_factory=list)
    items: list[AnalysisItemRow] = field(default_factory=list)


@dataclass(frozen=True)
class PriceComparison:
    code: str
    analysis_id: int
    analysis_name: str
    analysis_unit: str | None
    analysis_price: Decimal | None
    price_book_id: int
    price_book: str          # "Başlık 2026 Ek"
    price_unit: str
    book_price: Decimal
    difference: Decimal | None   # analiz fiyatı − kitap fiyatı (analiz fiyatı yoksa None)


_BOOK_SELECT = (
    "SELECT ab.*, a.name AS authority_name, sf.original_name AS source_name,"
    " (SELECT COUNT(*) FROM official_analyses oa WHERE oa.analysis_book_id = ab.id) AS analysis_count,"
    " (SELECT COUNT(*) FROM official_analyses oa WHERE oa.analysis_book_id = ab.id AND oa.check_ok = 1)"
    " AS consistent_count"
    " FROM analysis_books ab JOIN authorities a ON a.id = ab.authority_id"
    " LEFT JOIN source_files sf ON sf.id = ab.source_file_id"
)


def _book(r: sqlite3.Row) -> AnalysisBookRow:
    return AnalysisBookRow(r["id"], r["authority_id"], r["authority_name"], r["title"], r["year"], r["edition"],
                           r["source_file_id"], r["source_name"], r["created_at"], r["analysis_count"],
                           r["consistent_count"], r["period"], r["valid_from"])


def _analysis(r: sqlite3.Row, items: list[AnalysisItemRow] | None = None) -> AnalysisRow:
    d = from_db_text
    return AnalysisRow(r["id"], r["analysis_book_id"], r["code"], r["code_norm"], r["name"], r["unit"],
                       r["analysis_date"], r["group_label"], d(r["subtotal"]), d(r["material_total"]),
                       d(r["labour_total"]), d(r["overhead_rate"]), d(r["overhead_amount"]), d(r["unit_price"]),
                       d(r["montage_price"]), r["notes"], r["page"], bool(r["check_ok"]),
                       json.loads(r["warnings_json"] or "[]"), items or [])


def _item(r: sqlite3.Row) -> AnalysisItemRow:
    d = from_db_text
    return AnalysisItemRow(r["id"], r["line_no"], r["section"], r["code"], r["description"], r["unit"],
                           d(r["quantity"]), r["quantity_text"], d(r["unit_price"]), d(r["amount"]),
                           r["amount_column"])


# --- kitaplar --------------------------------------------------------------
def book_exists(conn: sqlite3.Connection, authority_id: int, year: int, edition: str, title: str) -> bool:
    return conn.execute("SELECT 1 FROM analysis_books WHERE authority_id = ? AND year = ? AND edition = ?"
                        " AND title = ?", (authority_id, year, edition, title)).fetchone() is not None


def books_for_source(conn: sqlite3.Connection, source_file_id: int) -> list[AnalysisBookRow]:
    return [_book(r) for r in conn.execute(_BOOK_SELECT + " WHERE ab.source_file_id = ? ORDER BY ab.id",
                                           (source_file_id,))]


def insert_book(conn: sqlite3.Connection, authority_id: int, title: str, year: int, edition: str,
                source_file_id: int | None, import_job_id: int | None = None, notes: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO analysis_books(authority_id, title, year, edition, source_file_id, import_job_id, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (authority_id, title, year, edition, source_file_id, import_job_id, notes)).lastrowid


def list_books(conn: sqlite3.Connection) -> list[AnalysisBookRow]:
    return [_book(r) for r in conn.execute(_BOOK_SELECT + " ORDER BY a.name, ab.year DESC, ab.title, ab.edition")]


def get_book(conn: sqlite3.Connection, book_id: int) -> AnalysisBookRow | None:
    r = conn.execute(_BOOK_SELECT + " WHERE ab.id = ?", (book_id,)).fetchone()
    return _book(r) if r else None


# --- analizler -------------------------------------------------------------
def insert_analysis(conn: sqlite3.Connection, book_id: int, values: dict) -> int:
    """values: tablo sütun adlarıyla (ondalıklar kanonik metin olarak hazırlanmış)."""
    cols = ["code", "code_norm", "name", "unit", "analysis_date", "group_label", "subtotal", "material_total",
            "labour_total", "overhead_rate", "overhead_amount", "unit_price", "montage_price", "notes", "page",
            "check_ok", "warnings_json"]
    return conn.execute(
        f"INSERT INTO official_analyses(analysis_book_id, {', '.join(cols)})"
        f" VALUES (?, {', '.join('?' for _ in cols)})", (book_id, *(values.get(c) for c in cols))).lastrowid


def insert_items(conn: sqlite3.Connection, analysis_id: int, items: Iterable[tuple]) -> None:
    """items: (line_no, section, code, description, unit, quantity, quantity_text, unit_price, amount, column)"""
    conn.executemany(
        "INSERT INTO official_analysis_items(analysis_id, line_no, section, code, description, unit, quantity,"
        " quantity_text, unit_price, amount, amount_column) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ((analysis_id, *it) for it in items))


def list_analyses(conn: sqlite3.Connection, book_id: int) -> list[AnalysisRow]:
    """Kitabın analizleri (bileşenler olmadan), poz koduna göre sıralı."""
    return [_analysis(r) for r in conn.execute(
        "SELECT * FROM official_analyses WHERE analysis_book_id = ? ORDER BY code_norm", (book_id,))]


def get_analysis(conn: sqlite3.Connection, analysis_id: int) -> AnalysisRow | None:
    r = conn.execute("SELECT * FROM official_analyses WHERE id = ?", (analysis_id,)).fetchone()
    if r is None:
        return None
    items = [_item(i) for i in conn.execute(
        "SELECT * FROM official_analysis_items WHERE analysis_id = ? ORDER BY line_no", (analysis_id,))]
    return _analysis(r, items)


def compare_with_prices(conn: sqlite3.Connection, book_id: int, same_authority: bool = True) -> list[PriceComparison]:
    """Analiz birim fiyatı ile kütüphanedeki aynı kodlu poz fiyatları (yalnızca okuma)."""
    sql = (
        "SELECT oa.id AS analysis_id, oa.code, oa.name, oa.unit AS a_unit, oa.unit_price AS a_price,"
        " pb.id AS pb_id, pb.title, pb.year, pb.edition, pp.unit AS p_unit, pp.unit_price AS p_price"
        " FROM official_analyses oa"
        " JOIN analysis_books ab ON ab.id = oa.analysis_book_id"
        " JOIN positions p ON p.code_norm = oa.code_norm"
        " JOIN position_prices pp ON pp.position_id = p.id"
        " JOIN price_books pb ON pb.id = pp.price_book_id"
        " WHERE oa.analysis_book_id = ? AND pb.hidden = 0"
        + (" AND p.authority_id = ab.authority_id" if same_authority else "")
        + " ORDER BY oa.code_norm, pb.year DESC, pb.id DESC"
    )
    out = []
    for r in conn.execute(sql, (book_id,)):
        a_price, p_price = from_db_text(r["a_price"]), from_db_text(r["p_price"])
        label = f"{r['title']} {r['year']}" + (f" {r['edition']}" if r["edition"] else "")
        out.append(PriceComparison(r["code"], r["analysis_id"], r["name"], r["a_unit"], a_price, r["pb_id"], label,
                                   r["p_unit"], p_price, None if a_price is None else a_price - p_price))
    return out
