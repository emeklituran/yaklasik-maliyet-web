"""Projeler ve keşif listeleri (başlık düzeyi)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    authority_id: int | None
    authority_name: str | None
    ikn: str | None
    tender_date: str | None
    work_type: str | None
    offer_type: str
    status: str
    notes: str | None
    estimate_count: int
    tender_model: str = "BIRIM_FIYAT"     # BIRIM_FIYAT | ANAHTAR_TESLIM | KARMA
    disciplines: str = ""                 # virgülle ayrılmış disiplin kodları


@dataclass(frozen=True)
class Estimate:
    id: int
    project_id: int
    name: str
    revision: int
    locked: bool
    created_at: str
    source_priority: list[int]
    item_count: int
    approved_count: int
    ym_date: str | None = None            # yaklaşık maliyet hesap tarihi (endeks güncellemesi)
    index_series_id: int | None = None    # varsayılan endeks serisi
    fx_rate_id: int | None = None         # döviz karşılığı için sabitlenen kur (exchange_rates)

    @property
    def display_name(self) -> str:
        return f"{self.name} (Rev. {self.revision})"


_PSELECT = ("SELECT p.*, a.name AS authority_name,"
            " (SELECT COUNT(*) FROM estimates e WHERE e.project_id = p.id) AS estimate_count"
            " FROM projects p LEFT JOIN authorities a ON a.id = p.authority_id")


def _project(r: sqlite3.Row) -> Project:
    return Project(r["id"], r["name"], r["authority_id"], r["authority_name"], r["ikn"], r["tender_date"],
                   r["work_type"], r["offer_type"], r["status"], r["notes"], r["estimate_count"],
                   r["tender_model"], r["disciplines"])


def list_projects(conn: sqlite3.Connection, include_archived: bool = True) -> list[Project]:
    sql = _PSELECT + ("" if include_archived else " WHERE p.status <> 'ARSIV'") + " ORDER BY p.created_at DESC, p.id DESC"
    return [_project(r) for r in conn.execute(sql)]


def get_project(conn: sqlite3.Connection, project_id: int) -> Project | None:
    r = conn.execute(_PSELECT + " WHERE p.id = ?", (project_id,)).fetchone()
    return _project(r) if r else None


def insert_project(conn: sqlite3.Connection, **f) -> int:
    return conn.execute(
        "INSERT INTO projects(name, authority_id, ikn, tender_date, work_type, offer_type, status, notes,"
        " tender_model, disciplines) VALUES (:name, :authority_id, :ikn, :tender_date, :work_type, :offer_type,"
        " :status, :notes, :tender_model, :disciplines)", f).lastrowid


def update_project(conn: sqlite3.Connection, project_id: int, **f) -> None:
    conn.execute(
        "UPDATE projects SET name = :name, authority_id = :authority_id, ikn = :ikn, tender_date = :tender_date,"
        " work_type = :work_type, offer_type = :offer_type, status = :status, notes = :notes,"
        " tender_model = :tender_model, disciplines = :disciplines WHERE id = :id",
        {**f, "id": project_id})


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


_ESELECT = ("SELECT e.*, (SELECT COUNT(*) FROM estimate_items i WHERE i.estimate_id = e.id) AS item_count,"
            " (SELECT COUNT(*) FROM estimate_items i WHERE i.estimate_id = e.id AND i.match_status = 'ONAYLANDI')"
            " AS approved_count FROM estimates e")


def _estimate(r: sqlite3.Row) -> Estimate:
    return Estimate(r["id"], r["project_id"], r["name"], r["revision"], bool(r["locked"]), r["created_at"],
                    json.loads(r["source_priority_json"] or "[]"), r["item_count"], r["approved_count"],
                    r["ym_date"], r["index_series_id"], r["fx_rate_id"])


def list_estimates(conn: sqlite3.Connection, project_id: int) -> list[Estimate]:
    return [_estimate(r) for r in conn.execute(_ESELECT + " WHERE e.project_id = ? ORDER BY e.name, e.revision DESC",
                                               (project_id,))]


def get_estimate(conn: sqlite3.Connection, estimate_id: int) -> Estimate | None:
    r = conn.execute(_ESELECT + " WHERE e.id = ?", (estimate_id,)).fetchone()
    return _estimate(r) if r else None


def insert_estimate(conn: sqlite3.Connection, project_id: int, name: str) -> int:
    rev = conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM estimates WHERE project_id = ? AND name = ?",
                       (project_id, name)).fetchone()[0]
    return conn.execute("INSERT INTO estimates(project_id, name, revision) VALUES (?, ?, ?)",
                        (project_id, name, rev)).lastrowid


def set_priority(conn: sqlite3.Connection, estimate_id: int, book_ids: list[int]) -> None:
    conn.execute("UPDATE estimates SET source_priority_json = ? WHERE id = ?", (json.dumps(book_ids), estimate_id))


def set_index_settings(conn: sqlite3.Connection, estimate_id: int, ym_date: str | None,
                       series_id: int | None) -> None:
    conn.execute("UPDATE estimates SET ym_date = ?, index_series_id = ? WHERE id = ?", (ym_date, series_id, estimate_id))


def set_locked(conn: sqlite3.Connection, estimate_id: int, locked: bool) -> None:
    conn.execute("UPDATE estimates SET locked = ? WHERE id = ?", (int(locked), estimate_id))


def estimate_has_final_calc(conn: sqlite3.Connection, estimate_id: int) -> bool:
    return conn.execute("SELECT 1 FROM calculations WHERE estimate_id = ? AND status = 'KESIN'",
                        (estimate_id,)).fetchone() is not None


def delete_estimate(conn: sqlite3.Connection, estimate_id: int) -> None:
    conn.execute("DELETE FROM calculations WHERE estimate_id = ? AND status = 'TASLAK'", (estimate_id,))
    conn.execute("DELETE FROM estimates WHERE id = ?", (estimate_id,))
