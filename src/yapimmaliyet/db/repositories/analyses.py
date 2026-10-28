"""Özel poz analizleri ve girdileri."""

from __future__ import annotations

import sqlite3

from yapimmaliyet.domain.analysis import AnalysisItemInput, AnalysisSpec, ItemType, ProfitBase
from yapimmaliyet.domain.money import from_db_text, to_db_text

RATE_PLACES = 6


def insert(conn: sqlite3.Connection, version_id: int, spec: AnalysisSpec) -> int:
    aid = conn.execute(
        "INSERT INTO analyses(custom_position_version_id, base_quantity, overhead_rate, profit_rate, profit_base,"
        " rule_source_text) VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, to_db_text(spec.base_quantity, RATE_PLACES), to_db_text(spec.overhead_rate, RATE_PLACES),
         to_db_text(spec.profit_rate, RATE_PLACES), spec.profit_base.value, spec.rule_source_text)).lastrowid
    conn.executemany(
        "INSERT INTO analysis_items(analysis_id, line_no, item_type, ref_position_price_id, description, unit,"
        " quantity, unit_price, waste_rate, coefficient, unit_conversion, source_text, ref_custom_position_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(aid, n, ItemType(it.item_type).value, it.ref_position_price_id, it.description.strip(), it.unit.strip(),
          to_db_text(it.quantity, RATE_PLACES), to_db_text(it.unit_price, 4), to_db_text(it.waste_rate, RATE_PLACES),
          to_db_text(it.coefficient, RATE_PLACES), to_db_text(it.unit_conversion, RATE_PLACES), it.source_text, it.ref_custom_position_id)
         for n, it in enumerate(spec.items, start=1)])
    return aid


def get_spec(conn: sqlite3.Connection, version_id: int) -> AnalysisSpec | None:
    a = conn.execute("SELECT * FROM analyses WHERE custom_position_version_id = ?", (version_id,)).fetchone()
    if a is None:
        return None
    items = tuple(
        AnalysisItemInput(ItemType(r["item_type"]), r["description"], r["unit"], from_db_text(r["quantity"]),
                          from_db_text(r["unit_price"]), from_db_text(r["waste_rate"]), from_db_text(r["coefficient"]),
                          from_db_text(r["unit_conversion"]), r["source_text"], r["ref_position_price_id"],
                          r["ref_custom_position_id"])
        for r in conn.execute("SELECT * FROM analysis_items WHERE analysis_id = ? ORDER BY line_no", (a["id"],)))
    return AnalysisSpec(from_db_text(a["base_quantity"]), from_db_text(a["overhead_rate"]),
                        from_db_text(a["profit_rate"]), ProfitBase(a["profit_base"]), a["rule_source_text"], items)
