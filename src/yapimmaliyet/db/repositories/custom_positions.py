"""Özel pozlar ve fiyat sürümleri."""

from __future__ import annotations

import re
import sqlite3

from yapimmaliyet.domain.models import PRICE_DB_PLACES, CustomPosition, CustomVersion, PriceMethod
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit

CODE_PREFIX = "ÖZ-"
_AUTO_CODE = re.compile(r"^ÖZ-(\d+)$")


def _version(r: sqlite3.Row) -> CustomVersion:
    return CustomVersion(
        r["id"], r["custom_position_id"], r["version_no"], r["description"], r["unit"],
        PriceMethod(r["price_method"]), from_db_text(r["direct_price"]),
        from_db_text(r["computed_price"]), r["price_date"], r["basis_text"], bool(r["locked"]),
        r["created_at"])


def _latest_version(conn: sqlite3.Connection, cp_id: int) -> CustomVersion | None:
    r = conn.execute("SELECT * FROM custom_position_versions WHERE custom_position_id = ?"
                     " ORDER BY version_no DESC LIMIT 1", (cp_id,)).fetchone()
    return _version(r) if r else None


def _position(conn: sqlite3.Connection, r: sqlite3.Row) -> CustomPosition:
    return CustomPosition(r["id"], r["code"], r["name"], r["category"], r["status"], r["project_id"],
                          r["created_at"], _latest_version(conn, r["id"]))


def list_positions(conn: sqlite3.Connection, text: str = "", include_passive: bool = False) -> list[CustomPosition]:
    sql, params = "SELECT * FROM custom_positions", []
    clauses = []
    for token in normalize_search(text).split():
        clauses.append("search_text LIKE ?")
        params.append(f"%{token}%")
    if not include_passive:
        clauses.append("status = 'AKTIF'")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY code"
    return [_position(conn, r) for r in conn.execute(sql, params).fetchall()]


def get(conn: sqlite3.Connection, cp_id: int) -> CustomPosition | None:
    r = conn.execute("SELECT * FROM custom_positions WHERE id = ?", (cp_id,)).fetchone()
    return _position(conn, r) if r else None


def versions(conn: sqlite3.Connection, cp_id: int) -> list[CustomVersion]:
    return [_version(r) for r in conn.execute(
        "SELECT * FROM custom_position_versions WHERE custom_position_id = ? ORDER BY version_no DESC", (cp_id,))]


def code_exists(conn: sqlite3.Connection, code: str) -> bool:
    return conn.execute("SELECT 1 FROM custom_positions WHERE code_norm = ?",
                        (normalize_code(code),)).fetchone() is not None


def next_code(conn: sqlite3.Connection) -> str:
    highest = 0
    for (code,) in conn.execute("SELECT code FROM custom_positions"):
        m = _AUTO_CODE.match(code)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{CODE_PREFIX}{highest + 1:04d}"


def _search_text(code: str, name: str, description: str) -> str:
    return normalize_search(f"{code} {name} {description}")


def insert_position(conn: sqlite3.Connection, code: str, name: str, category: str | None,
                    description: str) -> int:
    return conn.execute(
        "INSERT INTO custom_positions(code, code_norm, name, category, search_text) VALUES (?, ?, ?, ?, ?)",
        (code, normalize_code(code), name, category, _search_text(code, name, description))).lastrowid


def insert_direct_version(conn: sqlite3.Connection, cp_id: int, version_no: int, description: str,
                          unit: str, price, price_date: str, basis_text: str) -> int:
    return conn.execute(
        "INSERT INTO custom_position_versions(custom_position_id, version_no, description, unit, unit_norm,"
        " price_method, direct_price, price_date, basis_text) VALUES (?, ?, ?, ?, ?, 'DOGRUDAN', ?, ?, ?)",
        (cp_id, version_no, description, unit, normalize_unit(unit), to_db_text(price, PRICE_DB_PLACES),
         price_date, basis_text)).lastrowid


def lock_version(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE custom_position_versions SET locked = 1 WHERE id = ? AND locked = 0", (version_id,))


def insert_analysis_version(conn: sqlite3.Connection, cp_id: int, version_no: int, description: str, unit: str,
                            computed_price, price_date: str, basis_text: str) -> int:
    return conn.execute(
        "INSERT INTO custom_position_versions(custom_position_id, version_no, description, unit, unit_norm,"
        " price_method, direct_price, computed_price, price_date, basis_text)"
        " VALUES (?, ?, ?, ?, ?, 'ANALIZ', NULL, ?, ?, ?)",
        (cp_id, version_no, description, unit, normalize_unit(unit), to_db_text(computed_price, PRICE_DB_PLACES),
         price_date, basis_text)).lastrowid


def lock_versions(conn: sqlite3.Connection, cp_id: int) -> None:
    conn.execute("UPDATE custom_position_versions SET locked = 1 WHERE custom_position_id = ? AND locked = 0",
                 (cp_id,))


def update_info(conn: sqlite3.Connection, cp_id: int, name: str, category: str | None) -> None:
    cp = get(conn, cp_id)
    desc = cp.current.description if cp and cp.current else ""
    conn.execute("UPDATE custom_positions SET name = ?, category = ?, search_text = ? WHERE id = ?",
                 (name, category, _search_text(cp.code if cp else "", name, desc), cp_id))


def refresh_search_text(conn: sqlite3.Connection, cp_id: int) -> None:
    cp = get(conn, cp_id)
    if cp:
        desc = cp.current.description if cp.current else ""
        conn.execute("UPDATE custom_positions SET search_text = ? WHERE id = ?",
                     (_search_text(cp.code, cp.name, desc), cp_id))


def set_status(conn: sqlite3.Connection, cp_id: int, status: str) -> None:
    conn.execute("UPDATE custom_positions SET status = ? WHERE id = ?", (status, cp_id))
