"""İdareler."""

from __future__ import annotations

import sqlite3

from yapimmaliyet.domain.models import Authority


def _row(r: sqlite3.Row) -> Authority:
    return Authority(r["id"], r["name"], r["short_name"], r["notes"])


def list_all(conn: sqlite3.Connection) -> list[Authority]:
    return [_row(r) for r in conn.execute("SELECT * FROM authorities ORDER BY name")]


def get(conn: sqlite3.Connection, authority_id: int) -> Authority | None:
    r = conn.execute("SELECT * FROM authorities WHERE id = ?", (authority_id,)).fetchone()
    return _row(r) if r else None


def insert(conn: sqlite3.Connection, name: str, short_name: str | None, notes: str | None) -> int:
    return conn.execute("INSERT INTO authorities(name, short_name, notes) VALUES (?, ?, ?)",
                        (name, short_name, notes)).lastrowid


def update(conn: sqlite3.Connection, authority_id: int, name: str, short_name: str | None,
           notes: str | None) -> None:
    conn.execute("UPDATE authorities SET name = ?, short_name = ?, notes = ? WHERE id = ?",
                 (name, short_name, notes, authority_id))


def delete(conn: sqlite3.Connection, authority_id: int) -> None:
    conn.execute("DELETE FROM authorities WHERE id = ?", (authority_id,))


def usage_count(conn: sqlite3.Connection, authority_id: int) -> int:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM price_books WHERE authority_id = ?)"
        " + (SELECT COUNT(*) FROM projects WHERE authority_id = ?)",
        (authority_id, authority_id)).fetchone()
    return row[0]
