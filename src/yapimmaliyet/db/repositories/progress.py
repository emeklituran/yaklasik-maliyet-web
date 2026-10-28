"""Hakediş tabloları (pp_*): sözleşme, kalem, kesinti türü, hakediş, satır, metraj, kesinti ve durum geçmişi."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.progress import DeductionType, Item

PRICE_PLACES, QTY_PLACES, RATE_PLACES = 4, 10, 6   # götürü oranı büyük bedellerde 10 basamak ister


def _t(value: Decimal | None, places: int) -> str | None:
    return None if value is None else to_db_text(value, places)


@dataclass(frozen=True)
class Contract:
    id: int
    kind: str
    project_id: int | None
    title: str
    employer: str
    contractor: str
    contract_no: str | None
    contract_date: str | None
    ikn: str | None
    start_date: str | None
    end_date: str | None
    model: str
    contract_amount: Decimal
    currency: str
    vat_rate: Decimal
    vat_base: str
    advance_total: Decimal
    signatures: list
    notes: str | None
    is_sample: bool
    created_at: str


CONTRACT_FIELDS = ("kind", "project_id", "title", "employer", "contractor", "contract_no", "contract_date", "ikn",
                   "start_date", "end_date", "model", "contract_amount", "currency", "vat_rate", "vat_base",
                   "advance_total", "signatures", "notes", "is_sample")


def _contract(r: sqlite3.Row) -> Contract:
    return Contract(r["id"], r["kind"], r["project_id"], r["title"], r["employer"], r["contractor"], r["contract_no"],
                    r["contract_date"], r["ikn"], r["start_date"], r["end_date"], r["model"],
                    from_db_text(r["contract_amount"]), r["currency"], from_db_text(r["vat_rate"]), r["vat_base"],
                    from_db_text(r["advance_total"]), json.loads(r["signatures"]), r["notes"], bool(r["is_sample"]),
                    r["created_at"])


def _contract_values(data: dict) -> list:
    out = []
    for f in CONTRACT_FIELDS:
        v = data.get(f)
        if f in ("contract_amount", "advance_total"):
            v = to_db_text(v or 0, 2)
        elif f == "vat_rate":
            v = to_db_text(v, 4)
        elif f == "signatures":
            v = json.dumps(v or [], ensure_ascii=False)
        elif f == "is_sample":
            v = int(bool(v))
        out.append(v)
    return out


def list_contracts(conn: sqlite3.Connection, kind: str) -> list[Contract]:
    return [_contract(r) for r in conn.execute("SELECT * FROM pp_contracts WHERE kind = ? ORDER BY id", (kind,))]


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Contract | None:
    r = conn.execute("SELECT * FROM pp_contracts WHERE id = ?", (contract_id,)).fetchone()
    return _contract(r) if r else None


def insert_contract(conn: sqlite3.Connection, data: dict) -> int:
    cols = ", ".join(CONTRACT_FIELDS)
    marks = ", ".join("?" for _ in CONTRACT_FIELDS)
    return conn.execute(f"INSERT INTO pp_contracts({cols}) VALUES ({marks})", _contract_values(data)).lastrowid


def update_contract(conn: sqlite3.Connection, contract_id: int, data: dict) -> None:
    fields = [f for f in CONTRACT_FIELDS if f != "kind"]
    values = _contract_values(data)[1:]
    conn.execute(f"UPDATE pp_contracts SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                 values + [contract_id])


def delete_contract(conn: sqlite3.Connection, contract_id: int) -> None:
    conn.execute("DELETE FROM pp_contracts WHERE id = ?", (contract_id,))


# --- kalemler -------------------------------------------------------------------
ITEM_FIELDS = ("line_no", "code", "description", "unit", "discipline", "group_name", "method", "contract_qty",
               "unit_price", "lump_amount", "source", "source_ref", "estimate_item_id")


def _item(r: sqlite3.Row) -> Item:
    return Item(r["id"], r["line_no"], r["code"], r["description"], r["unit"], r["discipline"], r["group_name"],
                r["method"], from_db_text(r["contract_qty"]), from_db_text(r["unit_price"]),
                from_db_text(r["lump_amount"]), r["source"])


def _item_values(d: dict) -> list:
    return [d.get("line_no"), d.get("code"), d.get("description"), d.get("unit"), d.get("discipline"),
            d.get("group_name"), d.get("method"), _t(d.get("contract_qty"), QTY_PLACES),
            _t(d.get("unit_price"), PRICE_PLACES), _t(d.get("lump_amount"), 2), d.get("source") or "SOZLESME",
            d.get("source_ref"), d.get("estimate_item_id")]


def list_items(conn: sqlite3.Connection, contract_id: int) -> list[Item]:
    return [_item(r) for r in conn.execute("SELECT * FROM pp_items WHERE contract_id = ? ORDER BY line_no",
                                           (contract_id,))]


def get_item(conn: sqlite3.Connection, item_id: int) -> tuple[int, Item] | None:
    r = conn.execute("SELECT * FROM pp_items WHERE id = ?", (item_id,)).fetchone()
    return (r["contract_id"], _item(r)) if r else None


def item_source_ref(conn: sqlite3.Connection, item_id: int) -> str | None:
    r = conn.execute("SELECT source_ref FROM pp_items WHERE id = ?", (item_id,)).fetchone()
    return r[0] if r else None


def next_line_no(conn: sqlite3.Connection, contract_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(line_no), 0) + 1 FROM pp_items WHERE contract_id = ?",
                        (contract_id,)).fetchone()[0]


def insert_item(conn: sqlite3.Connection, contract_id: int, data: dict) -> int:
    cols = ", ".join(("contract_id",) + ITEM_FIELDS)
    marks = ", ".join("?" for _ in range(len(ITEM_FIELDS) + 1))
    return conn.execute(f"INSERT INTO pp_items({cols}) VALUES ({marks})", [contract_id] + _item_values(data)).lastrowid


def update_item(conn: sqlite3.Connection, item_id: int, data: dict) -> None:
    conn.execute(f"UPDATE pp_items SET {', '.join(f + ' = ?' for f in ITEM_FIELDS)} WHERE id = ?",
                 _item_values(data) + [item_id])


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM pp_items WHERE id = ?", (item_id,))


def item_used_in_approved(conn: sqlite3.Connection, item_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM pp_payment_lines l JOIN pp_payments p ON p.id = l.payment_id WHERE l.item_id = ? "
        "AND p.status IN ('ONAY', 'ODENDI', 'DUZELTILDI') LIMIT 1", (item_id,)).fetchone() is not None


def item_used(conn: sqlite3.Connection, item_id: int) -> bool:
    return conn.execute("SELECT 1 FROM pp_payment_lines WHERE item_id = ? UNION SELECT 1 FROM pp_measurements "
                        "WHERE item_id = ? LIMIT 1", (item_id, item_id)).fetchone() is not None


def insert_item_change(conn: sqlite3.Connection, item_id: int, field: str, old: str | None, new: str | None,
                       reason: str, ref: str | None) -> None:
    conn.execute("INSERT INTO pp_item_changes(item_id, field, old_value, new_value, reason, ref) VALUES (?,?,?,?,?,?)",
                 (item_id, field, old, new, reason, ref))


def item_changes(conn: sqlite3.Connection, contract_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT c.*, i.line_no, i.code, i.description FROM pp_item_changes c JOIN pp_items i "
                        "ON i.id = c.item_id WHERE i.contract_id = ? ORDER BY c.id", (contract_id,)).fetchall()


# --- kesinti türleri ------------------------------------------------------------
def _dtype(r: sqlite3.Row) -> DeductionType:
    return DeductionType(r["id"], r["name"], r["base"], from_db_text(r["rate"]), bool(r["is_advance"]),
                         bool(r["is_guarantee"]))


def list_deduction_types(conn: sqlite3.Connection, contract_id: int, active_only: bool = True) -> list[DeductionType]:
    sql = "SELECT * FROM pp_deduction_types WHERE contract_id = ?" + (" AND active = 1" if active_only else "")
    return [_dtype(r) for r in conn.execute(sql + " ORDER BY sort_order, id", (contract_id,))]


def deduction_type_rows(conn: sqlite3.Connection, contract_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pp_deduction_types WHERE contract_id = ? ORDER BY sort_order, id",
                        (contract_id,)).fetchall()


def insert_deduction_type(conn: sqlite3.Connection, contract_id: int, name: str, base: str, rate: Decimal | None,
                          is_advance: bool, is_guarantee: bool, sort_order: int, basis_note: str | None) -> int:
    return conn.execute(
        "INSERT INTO pp_deduction_types(contract_id, name, base, rate, is_advance, is_guarantee, sort_order, basis_note)"
        " VALUES (?,?,?,?,?,?,?,?)", (contract_id, name, base, _t(rate, RATE_PLACES), int(is_advance),
                                      int(is_guarantee), sort_order, basis_note)).lastrowid


def update_deduction_type(conn: sqlite3.Connection, type_id: int, name: str, base: str, rate: Decimal | None,
                          is_advance: bool, is_guarantee: bool, active: bool, basis_note: str | None) -> None:
    conn.execute("UPDATE pp_deduction_types SET name=?, base=?, rate=?, is_advance=?, is_guarantee=?, active=?, "
                 "basis_note=? WHERE id=?", (name, base, _t(rate, RATE_PLACES), int(is_advance), int(is_guarantee),
                                             int(active), basis_note, type_id))


def delete_deduction_type(conn: sqlite3.Connection, type_id: int) -> None:
    conn.execute("DELETE FROM pp_deduction_types WHERE id = ?", (type_id,))


def deduction_type_used(conn: sqlite3.Connection, type_id: int) -> bool:
    return conn.execute("SELECT 1 FROM pp_payment_deductions WHERE type_id = ? LIMIT 1", (type_id,)).fetchone() \
        is not None


# --- hakedişler -----------------------------------------------------------------
@dataclass(frozen=True)
class Payment:
    id: int
    contract_id: int
    no: int
    revision: int
    supersedes_id: int | None
    period_start: str | None
    period_end: str
    payment_date: str | None
    status: str
    price_diff_mode: str
    price_diff_amount: Decimal | None
    price_diff_params: dict
    price_diff_note: str | None
    notes: str | None
    snapshot_json: str | None
    snapshot_sha: str | None
    created_at: str

    @property
    def label(self) -> str:
        return f"{self.no} nolu hakediş" + (f" (rev. {self.revision})" if self.revision else "")


def _payment(r: sqlite3.Row) -> Payment:
    return Payment(r["id"], r["contract_id"], r["no"], r["revision"], r["supersedes_id"], r["period_start"],
                   r["period_end"], r["payment_date"], r["status"], r["price_diff_mode"],
                   from_db_text(r["price_diff_amount"]), json.loads(r["price_diff_params"] or "{}"),
                   r["price_diff_note"], r["notes"], r["snapshot_json"], r["snapshot_sha"], r["created_at"])


def list_payments(conn: sqlite3.Connection, contract_id: int) -> list[Payment]:
    return [_payment(r) for r in conn.execute("SELECT * FROM pp_payments WHERE contract_id = ? ORDER BY no, revision",
                                              (contract_id,))]


def get_payment(conn: sqlite3.Connection, payment_id: int) -> Payment | None:
    r = conn.execute("SELECT * FROM pp_payments WHERE id = ?", (payment_id,)).fetchone()
    return _payment(r) if r else None


def insert_payment(conn: sqlite3.Connection, contract_id: int, no: int, revision: int, supersedes_id: int | None,
                   period_start: str | None, period_end: str, payment_date: str | None, price_diff_mode: str = "YOK",
                   price_diff_amount: Decimal | None = None, price_diff_params: dict | None = None,
                   price_diff_note: str | None = None, notes: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO pp_payments(contract_id, no, revision, supersedes_id, period_start, period_end, payment_date, "
        "price_diff_mode, price_diff_amount, price_diff_params, price_diff_note, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (contract_id, no, revision, supersedes_id, period_start, period_end, payment_date, price_diff_mode,
         _t(price_diff_amount, 2), json.dumps(price_diff_params or {}, ensure_ascii=False), price_diff_note,
         notes)).lastrowid


def update_payment_header(conn: sqlite3.Connection, payment_id: int, period_start: str | None, period_end: str,
                          payment_date: str | None, price_diff_mode: str, price_diff_amount: Decimal | None,
                          price_diff_params: dict, price_diff_note: str | None, notes: str | None) -> None:
    conn.execute("UPDATE pp_payments SET period_start=?, period_end=?, payment_date=?, price_diff_mode=?, "
                 "price_diff_amount=?, price_diff_params=?, price_diff_note=?, notes=? WHERE id=?",
                 (period_start, period_end, payment_date, price_diff_mode, _t(price_diff_amount, 2),
                  json.dumps(price_diff_params, ensure_ascii=False), price_diff_note, notes, payment_id))


def set_status(conn: sqlite3.Connection, payment_id: int, status: str, snapshot_json: str | None = None,
               snapshot_sha: str | None = None, keep_snapshot: bool = False) -> None:
    if keep_snapshot:
        conn.execute("UPDATE pp_payments SET status = ? WHERE id = ?", (status, payment_id))
    else:
        conn.execute("UPDATE pp_payments SET status = ?, snapshot_json = ?, snapshot_sha = ? WHERE id = ?",
                     (status, snapshot_json, snapshot_sha, payment_id))


def delete_payment(conn: sqlite3.Connection, payment_id: int) -> None:
    conn.execute("DELETE FROM pp_payments WHERE id = ?", (payment_id,))


def log_status(conn: sqlite3.Connection, payment_id: int, from_status: str | None, to_status: str, user_name: str,
               note: str | None) -> None:
    conn.execute("INSERT INTO pp_status_log(payment_id, from_status, to_status, user_name, note) VALUES (?,?,?,?,?)",
                 (payment_id, from_status, to_status, user_name, note))


def status_log(conn: sqlite3.Connection, payment_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pp_status_log WHERE payment_id = ? ORDER BY id", (payment_id,)).fetchall()


# --- satırlar, metraj, kesintiler -----------------------------------------------
def line_qtys(conn: sqlite3.Connection, payment_id: int) -> dict[int, tuple[Decimal, str | None]]:
    return {r["item_id"]: (from_db_text(r["period_qty"]), r["note"])
            for r in conn.execute("SELECT * FROM pp_payment_lines WHERE payment_id = ?", (payment_id,))}


def set_line(conn: sqlite3.Connection, payment_id: int, item_id: int, qty: Decimal | None, note: str | None) -> None:
    if qty is None:
        conn.execute("DELETE FROM pp_payment_lines WHERE payment_id = ? AND item_id = ?", (payment_id, item_id))
        return
    conn.execute("INSERT INTO pp_payment_lines(payment_id, item_id, period_qty, note) VALUES (?,?,?,?) "
                 "ON CONFLICT(payment_id, item_id) DO UPDATE SET period_qty = excluded.period_qty, note = excluded.note",
                 (payment_id, item_id, to_db_text(qty, QTY_PLACES), note))


def measurements(conn: sqlite3.Connection, payment_id: int, item_id: int | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM pp_measurements WHERE payment_id = ?", [payment_id]
    if item_id is not None:
        sql += " AND item_id = ?"
        args.append(item_id)
    return conn.execute(sql + " ORDER BY item_id, line_no, id", args).fetchall()


def replace_measurements(conn: sqlite3.Connection, payment_id: int, item_id: int,
                         rows: list[tuple[str, Decimal, str | None]]) -> None:
    conn.execute("DELETE FROM pp_measurements WHERE payment_id = ? AND item_id = ?", (payment_id, item_id))
    for i, (desc, qty, ref) in enumerate(rows, start=1):
        conn.execute("INSERT INTO pp_measurements(payment_id, item_id, line_no, description, quantity, ref) "
                     "VALUES (?,?,?,?,?,?)", (payment_id, item_id, i, desc, to_db_text(qty, QTY_PLACES), ref))


def earlier_measurements(conn: sqlite3.Connection, contract_id: int, before_payment_id: int) -> list[sqlite3.Row]:
    """Aynı sözleşmenin onaylı (geçerli) önceki hakedişlerindeki metraj satırları — mükerrer imalat denetimi için."""
    return conn.execute(
        "SELECT m.*, p.no FROM pp_measurements m JOIN pp_payments p ON p.id = m.payment_id WHERE p.contract_id = ? "
        "AND p.id <> ? AND p.status IN ('ONAY', 'ODENDI')", (contract_id, before_payment_id)).fetchall()


def deduction_rows(conn: sqlite3.Connection, payment_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pp_payment_deductions WHERE payment_id = ? ORDER BY id", (payment_id,)).fetchall()


def replace_deductions(conn: sqlite3.Connection, payment_id: int, rows: list[tuple[int, str | None, Decimal]]) -> None:
    conn.execute("DELETE FROM pp_payment_deductions WHERE payment_id = ?", (payment_id,))
    for type_id, desc, amount in rows:
        conn.execute("INSERT INTO pp_payment_deductions(payment_id, type_id, description, amount) VALUES (?,?,?,?)",
                     (payment_id, type_id, desc, to_db_text(amount, 2)))


def copy_payment_content(conn: sqlite3.Connection, src_id: int, dst_id: int) -> None:
    for table, cols in (("pp_payment_lines", "item_id, period_qty, note"),
                        ("pp_measurements", "item_id, line_no, description, quantity, ref"),
                        ("pp_payment_deductions", "type_id, description, amount")):
        conn.execute(f"INSERT INTO {table}(payment_id, {cols}) SELECT ?, {cols} FROM {table} WHERE payment_id = ?",
                     (dst_id, src_id))
