"""Pozlar, kitap içi fiyat kayıtları, arama ve fiyat geçmişi."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from yapimmaliyet.domain.models import (
    PRICE_DB_PLACES,
    BookStatus,
    NewPositionPrice,
    PositionPrice,
    PriceHistoryEntry,
    SearchFilter,
)
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit

_SELECT = (
    "SELECT pp.id, pp.position_id, a.id AS authority_id, a.name AS authority_name,"
    " pb.id AS book_id, pb.title AS book_title, pb.year AS book_year, pb.edition AS book_edition,"
    " pb.status AS book_status, p.code, pp.description, pp.unit, pp.unit_price, p.category_path,"
    " pp.source_page, pp.source_row, pp.montage_price, pb.period, pb.valid_from, pb.price_kind, pb.verification,"
    " pb.source_ref, pp.printed_page, a.code AS authority_code"
    " FROM position_prices pp"
    " JOIN positions p ON p.id = pp.position_id"
    " JOIN price_books pb ON pb.id = pp.price_book_id"
    " JOIN authorities a ON a.id = pb.authority_id"
)

# Sıralama yalnızca bu beyaz listedeki ifadelerle yapılır (SQL enjeksiyonuna kapalı).
# Fiyat sıralamasındaki REAL dönüşümü yalnızca görüntüleme içindir; hesaplarda kullanılmaz.
_ORDER = {
    "authority": "a.name",
    "book": "pb.year, pb.edition",
    "code": "p.code_norm",
    "description": "pp.description",
    "unit": "pp.unit_norm",
    "price": "CAST(pp.unit_price AS REAL)",
}


def search_text_for(code: str, description: str) -> str:
    return normalize_search(f"{code} {description}")


def fts_query(text: str) -> str:
    """Kullanıcı metnini güvenli bir FTS5 sorgusuna çevirir: her kelime önek olarak, VE ile."""
    tokens = normalize_search(text).split()
    return " ".join(f'"{t}"*' for t in tokens)  # belirteçler yalnızca [0-9a-z] içerir


def _row(r: sqlite3.Row) -> PositionPrice:
    return PositionPrice(
        r["id"], r["position_id"], r["authority_id"], r["authority_name"], r["book_id"],
        r["book_title"], r["book_year"], r["book_edition"], BookStatus(r["book_status"]),
        r["code"], r["description"], r["unit"], from_db_text(r["unit_price"]),
        r["category_path"], r["source_page"], r["source_row"],
        None if r["montage_price"] is None else from_db_text(r["montage_price"]),
        r["period"], r["valid_from"], r["price_kind"], r["verification"], r["source_ref"], r["printed_page"],
        r["authority_code"],
    )


def _where(f: SearchFilter) -> tuple[str, list]:
    clauses, params = ["pb.hidden = 0"], []
    q = fts_query(f.text) if f.text else ""
    if q:
        clauses.append("pp.id IN (SELECT rowid FROM position_prices_fts WHERE position_prices_fts MATCH ?)")
        params.append(q)
    if f.authority_id is not None:
        clauses.append("pb.authority_id = ?")
        params.append(f.authority_id)
    if f.book_id is not None:
        clauses.append("pb.id = ?")
        params.append(f.book_id)
    if f.book_ids is not None:
        clauses.append("pb.id IN (%s)" % ", ".join("?" * len(f.book_ids)) if f.book_ids else "0")
        params.extend(f.book_ids)
    if f.exclude_drafts:
        clauses.append("pb.status <> 'TASLAK'")
    if f.year is not None:
        clauses.append("pb.year = ?")
        params.append(f.year)
    if f.category:
        clauses.append("p.category_path = ?")
        params.append(f.category)
    for column, value in (("pb.period", f.period), ("pb.price_kind", f.price_kind), ("pb.verification", f.verification)):
        if value:
            clauses.append(f"{column} = ?")   # sütun adları sabit listeden; değer parametre
            params.append(value)
    if not f.include_archived:
        clauses.append("pb.status <> 'ARSIV'")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def search(conn: sqlite3.Connection, f: SearchFilter, offset: int = 0, limit: int = 500) -> list[PositionPrice]:
    where, params = _where(f)
    order = _ORDER.get(f.sort, _ORDER["code"])
    direction = " DESC" if f.descending else ""
    order_sql = ", ".join(part.strip() + direction for part in order.split(","))
    sql = f"{_SELECT}{where} ORDER BY {order_sql}, pp.id LIMIT ? OFFSET ?"
    return [_row(r) for r in conn.execute(sql, [*params, limit, offset])]


def count(conn: sqlite3.Connection, f: SearchFilter) -> int:
    where, params = _where(f)
    sql = ("SELECT COUNT(*) FROM position_prices pp JOIN positions p ON p.id = pp.position_id"
           f" JOIN price_books pb ON pb.id = pp.price_book_id{where}")
    return conn.execute(sql, params).fetchone()[0]


def get(conn: sqlite3.Connection, price_id: int) -> PositionPrice | None:
    r = conn.execute(_SELECT + " WHERE pp.id = ?", (price_id,)).fetchone()
    return _row(r) if r else None


def codes_in_book(conn: sqlite3.Connection, book_id: int) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT p.code_norm FROM position_prices pp JOIN positions p ON p.id = pp.position_id"
        " WHERE pp.price_book_id = ?", (book_id,))}


def categories(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT category_path FROM positions WHERE category_path IS NOT NULL"
        " AND category_path <> '' ORDER BY category_path")]


def years(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT DISTINCT year FROM price_books WHERE hidden = 0 ORDER BY year DESC")]


def history(conn: sqlite3.Connection, position_id: int) -> list[PriceHistoryEntry]:
    rows = conn.execute(
        "SELECT pp.id, pb.id AS book_id, pb.title, pb.year, pb.edition, pb.status, pp.description, pp.unit,"
        " pp.unit_price, a.name AS authority_name, pb.period, pb.valid_from, pb.price_kind"
        " FROM position_prices pp JOIN price_books pb ON pb.id = pp.price_book_id"
        " JOIN authorities a ON a.id = pb.authority_id"
        " WHERE pp.position_id = ? AND pb.hidden = 0"
        " ORDER BY pb.year DESC, COALESCE(pb.valid_from, '') DESC, pb.edition DESC", (position_id,))
    return [PriceHistoryEntry(r["id"], r["book_id"], r["title"], r["year"], r["edition"],
                              BookStatus(r["status"]), r["description"], r["unit"],
                              from_db_text(r["unit_price"]), r["authority_name"], r["period"], r["valid_from"],
                              r["price_kind"]) for r in rows]


def periods(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT period FROM price_books WHERE hidden = 0 AND period IS NOT NULL AND period <> ''"
        " GROUP BY period ORDER BY MAX(year) DESC, period DESC")]


def _position_ids(conn: sqlite3.Connection, authority_id: int) -> dict[str, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT code_norm, id FROM positions WHERE authority_id = ?", (authority_id,))}


def insert_prices(conn: sqlite3.Connection, authority_id: int, book_id: int,
                  items: Iterable[NewPositionPrice], import_job_id: int | None = None) -> list[int]:
    """Kayıtları ekler; idarede olmayan poz kimlikleri oluşturulur. Transaction çağıranındır."""
    known = _position_ids(conn, authority_id)
    ids = []
    for it in items:
        code_norm = normalize_code(it.code)
        pos_id = known.get(code_norm)
        if pos_id is None:
            pos_id = conn.execute(
                "INSERT INTO positions(authority_id, code, code_norm, category_path) VALUES (?, ?, ?, ?)",
                (authority_id, it.code.strip(), code_norm, it.category_path)).lastrowid
            known[code_norm] = pos_id
        elif it.category_path:
            conn.execute("UPDATE positions SET category_path = ? WHERE id = ? AND category_path IS NULL",
                         (it.category_path, pos_id))
        ids.append(conn.execute(
            "INSERT INTO position_prices(price_book_id, position_id, description, unit, unit_norm,"
            " unit_price, search_text, source_page, source_row, raw_text, ocr_confidence, import_job_id,"
            " montage_price, printed_page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (book_id, pos_id, it.description.strip(), it.unit.strip(), normalize_unit(it.unit),
             to_db_text(it.unit_price, PRICE_DB_PLACES), search_text_for(it.code, it.description),
             it.source_page, it.source_row, it.raw_text, it.ocr_confidence, import_job_id,
             None if it.montage_price is None else to_db_text(it.montage_price, PRICE_DB_PLACES),
             it.printed_page)).lastrowid)
    return ids


def update_price(conn: sqlite3.Connection, price_id: int, code: str, description: str, unit: str,
                 unit_price, category_path: str | None) -> None:
    conn.execute(
        "UPDATE position_prices SET description = ?, unit = ?, unit_norm = ?, unit_price = ?, search_text = ?"
        " WHERE id = ?",
        (description.strip(), unit.strip(), normalize_unit(unit), to_db_text(unit_price, PRICE_DB_PLACES),
         search_text_for(code, description), price_id))
    conn.execute(
        "UPDATE positions SET category_path = ? WHERE id = (SELECT position_id FROM position_prices WHERE id = ?)",
        (category_path, price_id))


def reference_count(conn: sqlite3.Connection, price_id: int) -> int:
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM estimate_items WHERE position_price_id = ?)"
        " + (SELECT COUNT(*) FROM analysis_items WHERE ref_position_price_id = ?)",
        (price_id, price_id)).fetchone()[0]


def delete_price(conn: sqlite3.Connection, price_id: int) -> None:
    conn.execute("DELETE FROM position_prices WHERE id = ?", (price_id,))


def delete_orphan_positions(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "DELETE FROM positions WHERE NOT EXISTS (SELECT 1 FROM position_prices pp WHERE pp.position_id = positions.id)"
    ).rowcount
