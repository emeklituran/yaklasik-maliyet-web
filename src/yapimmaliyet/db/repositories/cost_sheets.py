"""Firma maliyeti çalışmaları, dolaylı giderler ve teklif senaryoları."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.firm_cost import COMPONENTS, IndirectCost, IndirectMethod, Scenario
from yapimmaliyet.domain.money import from_db_text, to_db_text


@dataclass(frozen=True)
class Sheet:
    id: int
    estimate_id: int
    name: str
    notes: str | None


def list_sheets(conn: sqlite3.Connection, estimate_id: int) -> list[Sheet]:
    return [Sheet(r["id"], r["estimate_id"], r["name"], r["notes"]) for r in conn.execute(
        "SELECT * FROM cost_sheets WHERE estimate_id = ? ORDER BY name", (estimate_id,))]


def get_sheet(conn: sqlite3.Connection, sheet_id: int) -> Sheet | None:
    r = conn.execute("SELECT * FROM cost_sheets WHERE id = ?", (sheet_id,)).fetchone()
    return Sheet(r["id"], r["estimate_id"], r["name"], r["notes"]) if r else None


def insert_sheet(conn: sqlite3.Connection, estimate_id: int, name: str) -> int:
    return conn.execute("INSERT INTO cost_sheets(estimate_id, name) VALUES (?, ?)", (estimate_id, name)).lastrowid


def copy_sheet(conn: sqlite3.Connection, sheet_id: int, name: str) -> int:
    """Çalışmayı bileşen satırları, dolaylı giderleri ve senaryolarıyla birlikte kopyalar."""
    src = get_sheet(conn, sheet_id)
    new_id = conn.execute("INSERT INTO cost_sheets(estimate_id, name, notes) VALUES (?, ?, ?)",
                          (src.estimate_id, name, src.notes)).lastrowid
    cols = ", ".join(COMPONENTS)
    conn.execute(f"INSERT INTO cost_sheet_lines(sheet_id, estimate_item_id, {cols}, note)"
                 f" SELECT ?, estimate_item_id, {cols}, note FROM cost_sheet_lines WHERE sheet_id = ?", (new_id, sheet_id))
    conn.execute("INSERT INTO cost_sheet_indirects(sheet_id, name, method, value, note)"
                 " SELECT ?, name, method, value, note FROM cost_sheet_indirects WHERE sheet_id = ? ORDER BY id",
                 (new_id, sheet_id))
    conn.execute("INSERT INTO offer_scenarios(sheet_id, name, profit_rate, risk_rate, notes)"
                 " SELECT ?, name, profit_rate, risk_rate, notes FROM offer_scenarios WHERE sheet_id = ? ORDER BY id",
                 (new_id, sheet_id))
    return new_id


def delete_sheet(conn: sqlite3.Connection, sheet_id: int) -> None:
    conn.execute("DELETE FROM cost_sheets WHERE id = ?", (sheet_id,))


def components(conn: sqlite3.Connection, sheet_id: int) -> dict[int, dict[str, Decimal]]:
    out = {}
    for r in conn.execute("SELECT * FROM cost_sheet_lines WHERE sheet_id = ?", (sheet_id,)):
        out[r["estimate_item_id"]] = {k: from_db_text(r[k]) for k in COMPONENTS}
    return out


def set_component(conn: sqlite3.Connection, sheet_id: int, item_id: int, key: str, value: Decimal) -> None:
    if key not in COMPONENTS:
        raise ValueError(key)
    conn.execute("INSERT OR IGNORE INTO cost_sheet_lines(sheet_id, estimate_item_id) VALUES (?, ?)", (sheet_id, item_id))
    conn.execute(f"UPDATE cost_sheet_lines SET {key} = ? WHERE sheet_id = ? AND estimate_item_id = ?",
                 (to_db_text(value, 4), sheet_id, item_id))


def indirects(conn: sqlite3.Connection, sheet_id: int) -> list[tuple[int, IndirectCost]]:
    return [(r["id"], IndirectCost(r["name"], IndirectMethod(r["method"]), from_db_text(r["value"]), r["note"]))
            for r in conn.execute("SELECT * FROM cost_sheet_indirects WHERE sheet_id = ? ORDER BY id", (sheet_id,))]


def insert_indirect(conn: sqlite3.Connection, sheet_id: int, ind: IndirectCost) -> int:
    return conn.execute("INSERT INTO cost_sheet_indirects(sheet_id, name, method, value, note) VALUES (?, ?, ?, ?, ?)",
                        (sheet_id, ind.name, ind.method.value, to_db_text(ind.value, 6), ind.note)).lastrowid


def delete_indirect(conn: sqlite3.Connection, indirect_id: int) -> None:
    conn.execute("DELETE FROM cost_sheet_indirects WHERE id = ?", (indirect_id,))


def scenarios(conn: sqlite3.Connection, sheet_id: int) -> list[tuple[int, Scenario]]:
    return [(r["id"], Scenario(r["name"], Decimal(r["profit_rate"]), Decimal(r["risk_rate"]), r["notes"]))
            for r in conn.execute("SELECT * FROM offer_scenarios WHERE sheet_id = ? ORDER BY id", (sheet_id,))]


def insert_scenario(conn: sqlite3.Connection, sheet_id: int, s: Scenario) -> int:
    return conn.execute("INSERT INTO offer_scenarios(sheet_id, name, profit_rate, risk_rate, notes) VALUES (?, ?, ?, ?, ?)",
                        (sheet_id, s.name, to_db_text(s.profit_rate, 6), to_db_text(s.risk_rate, 6),
                         s.notes)).lastrowid


def delete_scenario(conn: sqlite3.Connection, scenario_id: int) -> None:
    conn.execute("DELETE FROM offer_scenarios WHERE id = ?", (scenario_id,))
