"""Kitaplardaki oran pozları (Ölçü: %)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.text_tr import normalize_code


@dataclass(frozen=True)
class RateRow:
    id: int
    book_id: int
    book_label: str
    code: str
    description: str
    rate: Decimal
    source_page: int | None


def insert_many(conn: sqlite3.Connection, book_id: int, rates, import_job_id: int | None = None) -> int:
    """`rates`: code, description, rate, page alanlı nesneler. Kitapta zaten olan kod atlanır."""
    n = 0
    for r in rates:
        cur = conn.execute(
            "INSERT OR IGNORE INTO rate_positions(price_book_id, code, code_norm, description, rate, source_page,"
            " import_job_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (book_id, r.code, normalize_code(r.code), r.description, to_db_text(r.rate, 2), r.page, import_job_id))
        n += cur.rowcount
    return n


def copy(conn: sqlite3.Connection, src_book_id: int, dst_book_id: int) -> int:
    return conn.execute(
        "INSERT OR IGNORE INTO rate_positions(price_book_id, code, code_norm, description, rate, source_page)"
        " SELECT ?, code, code_norm, description, rate, source_page FROM rate_positions WHERE price_book_id = ?",
        (dst_book_id, src_book_id)).rowcount


def for_books(conn: sqlite3.Connection, book_ids: list[int]) -> list[RateRow]:
    if not book_ids:
        return []
    marks = ",".join("?" * len(book_ids))
    rows = conn.execute(
        "SELECT r.id, r.price_book_id, r.code, r.description, r.rate, r.source_page, pb.title, pb.year, pb.edition"
        f" FROM rate_positions r JOIN price_books pb ON pb.id = r.price_book_id WHERE r.price_book_id IN ({marks})"
        " ORDER BY r.code_norm", book_ids)
    return [RateRow(r["id"], r["price_book_id"], f"{r['title']} {r['year']} {r['edition']}".strip(), r["code"],
                    r["description"], from_db_text(r["rate"]), r["source_page"]) for r in rows]


def get(conn: sqlite3.Connection, rate_id: int) -> RateRow | None:
    r = conn.execute("SELECT price_book_id FROM rate_positions WHERE id = ?", (rate_id,)).fetchone()
    if r is None:
        return None
    return next(x for x in for_books(conn, [r[0]]) if x.id == rate_id)
