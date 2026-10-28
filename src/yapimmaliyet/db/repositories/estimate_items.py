"""Keşif kalemleri ve eşleştikleri fiyat kaynakları."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.text_tr import normalize_unit


@dataclass(frozen=True)
class EstimateItem:
    id: int
    estimate_id: int
    line_no: int
    work_group: str | None
    code: str | None
    description: str | None
    unit: str | None
    quantity: Decimal
    file_unit_price: Decimal | None
    file_amount: Decimal | None
    notes: str | None
    match_status: str
    position_price_id: int | None
    custom_version_id: int | None
    unit_mismatch_ack: bool
    user_price_override: Decimal | None
    matched_by: str | None
    # eşleşen kaynak
    matched_code: str | None
    matched_description: str | None
    matched_unit: str | None
    matched_price: Decimal | None
    matched_origin: str          # KITAP | OZEL_DOGRUDAN | OZEL_ANALIZ | YOK
    source_label: str
    price_date: str | None
    book_id: int | None
    index_series_id: int | None = None   # kalemin endeks sınıfı (boşsa keşifteki varsayılan)
    price_valid_from: str | None = None  # fiyatın yürürlük tarihi (kitap yürürlük tarihi / özel poz fiyat tarihi)
    discipline: str | None = None        # INSAAT | ELEKTRIK | MEKANIK | ALTYAPI
    pricing_method: str | None = None    # karma modelde BIRIM_FIYAT | GOTURU
    price_authority: str | None = None   # fiyat kaynağı kurumu (kopyadan; özel pozda "Özel poz")
    price_period: str | None = None      # fiyat dönemi (kopyadan); bilinmiyorsa None

    @property
    def unit_mismatch(self) -> bool:
        if not self.unit or not self.matched_unit:
            return False
        return normalize_unit(self.unit) != normalize_unit(self.matched_unit)


_SELECT = """
SELECT ei.*,
       p.code AS b_code, pp.description AS b_desc, pp.unit AS b_unit, pp.unit_price AS b_price,
       pb.id AS b_book, pb.title AS b_title, pb.year AS b_year, pb.edition AS b_edition, pb.valid_from AS b_valid,
       pb.period AS b_period,
       a.short_name AS b_auth_short, a.name AS b_auth,
       cp.code AS c_code, cp.name AS c_name, cv.description AS c_desc, cv.unit AS c_unit, cv.price_method AS c_method,
       cv.direct_price AS c_direct, cv.computed_price AS c_computed, cv.price_date AS c_date, cv.version_no AS c_ver
FROM estimate_items ei
LEFT JOIN position_prices pp ON pp.id = ei.position_price_id
LEFT JOIN positions p ON p.id = pp.position_id
LEFT JOIN price_books pb ON pb.id = pp.price_book_id
LEFT JOIN authorities a ON a.id = pb.authority_id
LEFT JOIN custom_position_versions cv ON cv.id = ei.custom_version_id
LEFT JOIN custom_positions cp ON cp.id = cv.custom_position_id
"""


def _row(r: sqlite3.Row) -> EstimateItem:
    if r["position_price_id"] is not None and r["snap_code"] is not None:
        # kütüphaneden çekilirken saklanan kopya: kütüphane değişse / kitap silinse de keşif aynı kalır
        m = (r["snap_code"], r["snap_description"], r["snap_unit"], from_db_text(r["snap_price"]), "KITAP",
             r["snap_source"], r["snap_valid"] or str(r["snap_year"]), r["snap_book_id"])
        valid = r["snap_valid"]
        src = (r["snap_authority"], r["snap_period"])
    elif r["position_price_id"] is not None:
        ed = f" {r['b_edition']}" if r["b_edition"] else ""
        label = f"{r['b_auth_short'] or r['b_auth']} — {r['b_title']} {r['b_year']}{ed}"
        m = (r["b_code"], r["b_desc"], r["b_unit"], from_db_text(r["b_price"]), "KITAP", label,
             r["b_valid"] or str(r["b_year"]), r["b_book"])
        valid = r["b_valid"]
        src = (r["b_auth_short"] or r["b_auth"], r["b_period"])
    elif r["custom_version_id"] is not None:
        analysis = r["c_method"] == "ANALIZ"
        price = from_db_text(r["c_computed"] if analysis else r["c_direct"])
        m = (r["c_code"], r["c_desc"], r["c_unit"], price, "OZEL_ANALIZ" if analysis else "OZEL_DOGRUDAN",
             f"Özel poz {r['c_code']} (sürüm {r['c_ver']})", r["c_date"], None)
        valid = r["c_date"]
        src = ("Özel poz", None)
    else:
        m = (None, None, None, None, "YOK", "", None, None)
        valid = None
        src = (None, None)
    return EstimateItem(
        r["id"], r["estimate_id"], r["line_no"], r["work_group"], r["raw_code"], r["raw_description"], r["raw_unit"],
        from_db_text(r["quantity"]), from_db_text(r["raw_unit_price"]), from_db_text(r["raw_amount"]), r["notes"],
        r["match_status"], r["position_price_id"], r["custom_version_id"], bool(r["unit_mismatch_ack"]),
        from_db_text(r["user_price_override"]), r["matched_by"], *m, r["index_series_id"], valid,
        r["discipline"], r["pricing_method"], *src)


def list_items(conn: sqlite3.Connection, estimate_id: int) -> list[EstimateItem]:
    return [_row(r) for r in conn.execute(_SELECT + " WHERE ei.estimate_id = ? ORDER BY ei.line_no", (estimate_id,))]


def get_item(conn: sqlite3.Connection, item_id: int) -> EstimateItem | None:
    r = conn.execute(_SELECT + " WHERE ei.id = ?", (item_id,)).fetchone()
    return _row(r) if r else None


def next_line_no(conn: sqlite3.Connection, estimate_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(line_no), 0) + 1 FROM estimate_items WHERE estimate_id = ?",
                        (estimate_id,)).fetchone()[0]


def insert_item(conn: sqlite3.Connection, estimate_id: int, line_no: int, work_group: str | None, code: str | None,
                description: str | None, unit: str | None, quantity: Decimal, notes: str | None,
                discipline: str | None = None, pricing_method: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO estimate_items(estimate_id, line_no, work_group, raw_code, raw_description, raw_unit, quantity,"
        " notes, discipline, pricing_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (estimate_id, line_no, work_group, code, description, unit, to_db_text(quantity, 3), notes, discipline,
         pricing_method)).lastrowid


def update_item(conn: sqlite3.Connection, item_id: int, work_group: str | None, code: str | None,
                description: str | None, unit: str | None, quantity: Decimal, notes: str | None) -> None:
    conn.execute("UPDATE estimate_items SET work_group = ?, raw_code = ?, raw_description = ?, raw_unit = ?,"
                 " quantity = ?, notes = ?, unit_mismatch_ack = 0 WHERE id = ?",
                 (work_group, code, description, unit, to_db_text(quantity, 3), notes, item_id))


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM estimate_items WHERE id = ?", (item_id,))


def take_snapshot(conn: sqlite3.Connection, item_id: int, price_id: int | None) -> None:
    """Kütüphane pozunun o anki bilgisini kalemin kendi kopyasına yazar (price_id None: kopyayı temizler)."""
    vals = (None,) * 10
    if price_id is not None:
        r = conn.execute(
            "SELECT p.code, pp.description, pp.unit, pp.unit_price, pb.id AS book_id, pb.title, pb.year, pb.edition,"
            " pb.valid_from, pb.period, COALESCE(a.short_name, a.name) AS auth FROM position_prices pp"
            " JOIN positions p ON p.id = pp.position_id JOIN price_books pb ON pb.id = pp.price_book_id"
            " JOIN authorities a ON a.id = pb.authority_id WHERE pp.id = ?", (price_id,)).fetchone()
        if r is not None:
            ed = f" {r['edition']}" if r["edition"] else ""
            vals = (r["code"], r["description"], r["unit"], r["unit_price"], f"{r['auth']} — {r['title']} {r['year']}{ed}",
                    r["valid_from"], r["book_id"], r["year"], r["auth"], r["period"])
    conn.execute("UPDATE estimate_items SET snap_code = ?, snap_description = ?, snap_unit = ?, snap_price = ?,"
                 " snap_source = ?, snap_valid = ?, snap_book_id = ?, snap_year = ?, snap_authority = ?, snap_period = ?"
                 " WHERE id = ?", (*vals, item_id))


def set_match(conn: sqlite3.Connection, item_id: int, status: str, position_price_id: int | None,
              custom_version_id: int | None, unit_ack: bool, matched_by: str | None) -> None:
    before = conn.execute("SELECT position_price_id, snap_code FROM estimate_items WHERE id = ?", (item_id,)).fetchone()
    conn.execute(
        "UPDATE estimate_items SET match_status = ?, position_price_id = ?, custom_version_id = ?,"
        " unit_mismatch_ack = ?, matched_by = ?,"
        " matched_at = CASE WHEN ? IS NULL THEN NULL ELSE strftime('%Y-%m-%dT%H:%M:%S','now','localtime') END"
        " WHERE id = ?",
        (status, position_price_id, custom_version_id, int(unit_ack), matched_by, matched_by, item_id))
    # kopya yalnız poz değişince (ya da hiç alınmamışsa) alınır; onay, kütüphanedeki sonraki değişikliği keşfe taşımaz
    if before is not None and (before["position_price_id"] != position_price_id or
                               (position_price_id is not None and before["snap_code"] is None)):
        take_snapshot(conn, item_id, position_price_id)


def set_index_series(conn: sqlite3.Connection, item_ids: list[int], series_id: int | None) -> None:
    conn.executemany("UPDATE estimate_items SET index_series_id = ? WHERE id = ?", [(series_id, i) for i in item_ids])


def set_classification(conn: sqlite3.Connection, item_ids: list[int], column: str, value: str | None) -> None:
    if column not in ("discipline", "pricing_method"):
        raise ValueError(column)
    conn.executemany(f"UPDATE estimate_items SET {column} = ? WHERE id = ?", [(value, i) for i in item_ids])


def set_override(conn: sqlite3.Connection, item_id: int, price: Decimal | None) -> None:
    conn.execute("UPDATE estimate_items SET user_price_override = ? WHERE id = ?",
                 (None if price is None else to_db_text(price, 4), item_id))
