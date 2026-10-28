"""Sözleşme değişiklik kayıtları (pp_contract_changes): iş artışı / eksilişi, süre uzatımı, ek protokol."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text, to_db_text

FIELDS = ("change_no", "kind", "title", "approval_date", "approval_ref", "amount_delta", "days_delta", "notes",
          "status")


@dataclass(frozen=True)
class Change:
    id: int
    contract_id: int
    change_no: int
    kind: str
    title: str
    approval_date: str | None
    approval_ref: str | None
    amount_delta: Decimal | None
    days_delta: int | None
    notes: str | None
    created_at: str
    status: str = "ONAYLI"

    @property
    def approved(self) -> bool:
        return self.status == "ONAYLI"


def _change(r: sqlite3.Row) -> Change:
    return Change(r["id"], r["contract_id"], r["change_no"], r["kind"], r["title"], r["approval_date"],
                  r["approval_ref"], None if r["amount_delta"] is None else from_db_text(r["amount_delta"]),
                  r["days_delta"], r["notes"], r["created_at"], r["status"])


def _values(d: dict) -> list:
    return [to_db_text(d[f], 2) if f == "amount_delta" and d[f] is not None else d.get(f) for f in FIELDS]


def list_changes(conn: sqlite3.Connection, contract_id: int) -> list[Change]:
    return [_change(r) for r in conn.execute(
        "SELECT * FROM pp_contract_changes WHERE contract_id = ? ORDER BY change_no", (contract_id,))]


def get_change(conn: sqlite3.Connection, change_id: int) -> Change | None:
    r = conn.execute("SELECT * FROM pp_contract_changes WHERE id = ?", (change_id,)).fetchone()
    return _change(r) if r else None


def next_no(conn: sqlite3.Connection, contract_id: int) -> int:
    return conn.execute("SELECT COALESCE(MAX(change_no), 0) + 1 FROM pp_contract_changes WHERE contract_id = ?",
                        (contract_id,)).fetchone()[0]


def insert_change(conn: sqlite3.Connection, contract_id: int, d: dict) -> int:
    return conn.execute(f"INSERT INTO pp_contract_changes(contract_id, {', '.join(FIELDS)}) "
                        f"VALUES (?, {', '.join('?' for _ in FIELDS)})", [contract_id] + _values(d)).lastrowid


def update_change(conn: sqlite3.Connection, change_id: int, d: dict) -> None:
    conn.execute(f"UPDATE pp_contract_changes SET {', '.join(f + ' = ?' for f in FIELDS)} WHERE id = ?",
                 _values(d) + [change_id])


def delete_change(conn: sqlite3.Connection, change_id: int) -> None:
    conn.execute("DELETE FROM pp_contract_changes WHERE id = ?", (change_id,))


def counts(conn: sqlite3.Connection) -> dict[int, int]:
    return dict(conn.execute("SELECT contract_id, COUNT(*) FROM pp_contract_changes GROUP BY contract_id"))
