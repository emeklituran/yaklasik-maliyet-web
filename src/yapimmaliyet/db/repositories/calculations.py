"""Hesap profilleri, kesinleştirilmiş hesaplar ve değişmez kayıtlar (snapshot)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    id: int
    kind: str
    name: str
    version: int
    params: dict
    legal_basis_text: str
    verified: bool
    valid_from: str | None
    imported_from: str | None

    @property
    def display_name(self) -> str:
        flag = "" if self.verified else " — DOĞRULANMAMIŞ"
        return f"{self.name} (s.{self.version}){flag}"


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    calculation_id: int
    created_at: str
    kind: str
    name: str | None
    profile_name: str
    sha256: str


def _profile(r: sqlite3.Row) -> Profile:
    return Profile(r["id"], r["kind"], r["name"], r["version"], json.loads(r["params_json"]), r["legal_basis_text"],
                   bool(r["verified"]), r["valid_from"], r["imported_from"])


def list_profiles(conn: sqlite3.Connection, kind: str | None = None) -> list[Profile]:
    # Profiller ilk eklenme sırasıyla listelenir (ilk profil arayüzde varsayılan seçimdir); aynı adın
    # sürümleri arasında en yenisi önce gelir.
    sql, params = ("SELECT p.* FROM calc_profiles p JOIN (SELECT name, MIN(id) AS first_id FROM calc_profiles"
                   " GROUP BY name) f ON f.name = p.name"), []
    if kind:
        sql += " WHERE p.kind = ?"
        params.append(kind)
    return [_profile(r) for r in conn.execute(sql + " ORDER BY p.kind, f.first_id, p.version DESC", params)]


def get_profile(conn: sqlite3.Connection, profile_id: int) -> Profile | None:
    r = conn.execute("SELECT * FROM calc_profiles WHERE id = ?", (profile_id,)).fetchone()
    return _profile(r) if r else None


def next_profile_version(conn: sqlite3.Connection, name: str) -> int:
    return conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM calc_profiles WHERE name = ?", (name,)).fetchone()[0]


def insert_profile(conn: sqlite3.Connection, kind: str, name: str, version: int, params: dict, legal_basis: str,
                   verified: bool, valid_from: str | None, imported_from: str | None) -> int:
    return conn.execute(
        "INSERT INTO calc_profiles(kind, name, version, params_json, legal_basis_text, verified, valid_from,"
        " imported_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, name, version, json.dumps(params, ensure_ascii=False), legal_basis, int(verified), valid_from,
         imported_from)).lastrowid


def insert_calculation(conn: sqlite3.Connection, estimate_id: int, profile_id: int, kind: str, name: str | None,
                       priority: list[int]) -> int:
    return conn.execute(
        "INSERT INTO calculations(estimate_id, profile_id, kind, name, source_priority_json, status)"
        " VALUES (?, ?, ?, ?, ?, 'KESIN')", (estimate_id, profile_id, kind, name, json.dumps(priority))).lastrowid


def insert_snapshot(conn: sqlite3.Connection, calculation_id: int, app_version: str, schema_version: int,
                    payload_json: str, sha256: str) -> int:
    return conn.execute(
        "INSERT INTO calculation_snapshots(calculation_id, app_version, schema_version, payload_json, sha256)"
        " VALUES (?, ?, ?, ?, ?)", (calculation_id, app_version, schema_version, payload_json, sha256)).lastrowid


def list_snapshots(conn: sqlite3.Connection, estimate_id: int) -> list[SnapshotInfo]:
    rows = conn.execute(
        "SELECT s.id, s.calculation_id, s.created_at, s.sha256, c.kind, c.name, p.name AS pname, p.version AS pver"
        " FROM calculation_snapshots s JOIN calculations c ON c.id = s.calculation_id"
        " JOIN calc_profiles p ON p.id = c.profile_id WHERE c.estimate_id = ? ORDER BY s.id DESC", (estimate_id,))
    return [SnapshotInfo(r["id"], r["calculation_id"], r["created_at"], r["kind"], r["name"],
                         f"{r['pname']} (s.{r['pver']})", r["sha256"]) for r in rows]


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM calculation_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
