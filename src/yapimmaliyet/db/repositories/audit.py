"""Değişiklik kaydı (audit_log)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _dump(data: dict[str, Any] | None) -> str | None:
    return None if data is None else json.dumps(data, ensure_ascii=False, default=str)


def log(conn: sqlite3.Connection, entity: str, entity_id: int | None, action: str,
        old: dict[str, Any] | None = None, new: dict[str, Any] | None = None,
        note: str | None = None) -> None:
    from yapimmaliyet.infra import authz

    authz.check_entity(entity, action)   # yetkisizse PermissionDenied: çağıran transaction geri alınır
    conn.execute(
        "INSERT INTO audit_log(entity, entity_id, action, old_json, new_json, note) VALUES (?, ?, ?, ?, ?, ?)",
        (entity, entity_id, action, _dump(old), _dump(new), note),
    )


def recent(conn: sqlite3.Connection, entity: str | None = None, entity_id: int | None = None,
           limit: int = 100) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM audit_log", []
    clauses = []
    if entity is not None:
        clauses.append("entity = ?")
        params.append(entity)
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
