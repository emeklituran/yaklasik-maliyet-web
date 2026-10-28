"""İhale teklifleri ve teklif satırları."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text, to_db_text


@dataclass(frozen=True)
class BidRow:
    id: int
    estimate_id: int
    bidder: str
    bid_date: str | None
    lump_total: Decimal | None
    is_sample: bool
    notes: str | None
    created_at: str


def _row(r: sqlite3.Row) -> BidRow:
    return BidRow(r["id"], r["estimate_id"], r["bidder"], r["bid_date"], from_db_text(r["lump_total"]),
                  bool(r["is_sample"]), r["notes"], r["created_at"])


def list_bids(conn: sqlite3.Connection, estimate_id: int) -> list[BidRow]:
    return [_row(r) for r in conn.execute("SELECT * FROM tender_bids WHERE estimate_id = ? ORDER BY id",
                                          (estimate_id,))]


def get_bid(conn: sqlite3.Connection, bid_id: int) -> BidRow | None:
    r = conn.execute("SELECT * FROM tender_bids WHERE id = ?", (bid_id,)).fetchone()
    return _row(r) if r else None


def insert_bid(conn: sqlite3.Connection, estimate_id: int, bidder: str, bid_date: str | None,
               lump_total: Decimal | None, is_sample: bool, notes: str | None) -> int:
    return conn.execute(
        "INSERT INTO tender_bids(estimate_id, bidder, bid_date, lump_total, is_sample, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (estimate_id, bidder, bid_date, None if lump_total is None else to_db_text(lump_total, 2), int(is_sample),
         notes)).lastrowid


def update_bid(conn: sqlite3.Connection, bid_id: int, bidder: str, bid_date: str | None,
               lump_total: Decimal | None, notes: str | None) -> None:
    conn.execute("UPDATE tender_bids SET bidder = ?, bid_date = ?, lump_total = ?, notes = ? WHERE id = ?",
                 (bidder, bid_date, None if lump_total is None else to_db_text(lump_total, 2), notes, bid_id))


def delete_bid(conn: sqlite3.Connection, bid_id: int) -> None:
    conn.execute("DELETE FROM tender_bids WHERE id = ?", (bid_id,))


def lines(conn: sqlite3.Connection, bid_id: int) -> dict[int, tuple[Decimal | None, Decimal | None]]:
    """Kalem kimliği → (birim fiyat, götürü tutar)."""
    return {r["estimate_item_id"]: (from_db_text(r["unit_price"]), from_db_text(r["lump_sum"]))
            for r in conn.execute("SELECT * FROM tender_bid_lines WHERE bid_id = ?", (bid_id,))}


def replace_lines(conn: sqlite3.Connection, bid_id: int,
                  rows: list[tuple[int, Decimal | None, Decimal | None]]) -> None:
    conn.execute("DELETE FROM tender_bid_lines WHERE bid_id = ?", (bid_id,))
    conn.executemany(
        "INSERT INTO tender_bid_lines(bid_id, estimate_item_id, unit_price, lump_sum) VALUES (?, ?, ?, ?)",
        [(bid_id, item, None if up is None else to_db_text(up, 4), None if ls is None else to_db_text(ls, 2))
         for item, up, ls in rows])
