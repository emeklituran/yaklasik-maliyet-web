"""Kaynak dosyalar, içe aktarma işleri, sorunlar ve eşleme şablonları."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ImportJobRow:
    id: int
    kind: str
    status: str
    started_at: str
    finished_at: str | None
    file_name: str | None
    target: str | None
    stats: dict
    issue_count: int


def source_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_files WHERE sha256 = ?", (sha256,)).fetchone()


def insert_source(conn: sqlite3.Connection, name: str, sha256: str, stored_path: str, size: int) -> int:
    return conn.execute("INSERT INTO source_files(original_name, sha256, stored_path, size_bytes) VALUES (?, ?, ?, ?)",
                        (name, sha256, stored_path, size)).lastrowid


def jobs_for_source(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM import_jobs WHERE source_file_id = ? AND status = 'TAMAMLANDI'"
                        " ORDER BY id", (source_id,)).fetchall()


def insert_job(conn: sqlite3.Connection, kind: str, source_id: int, mapping: dict, stats: dict,
               target_book_id: int | None = None, target_estimate_id: int | None = None) -> int:
    return conn.execute(
        "INSERT INTO import_jobs(kind, source_file_id, status, mapping_json, stats_json, target_book_id,"
        " target_estimate_id, finished_at) VALUES (?, ?, 'TAMAMLANDI', ?, ?, ?, ?,"
        " strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))",
        (kind, source_id, json.dumps(mapping, ensure_ascii=False), json.dumps(stats, ensure_ascii=False),
         target_book_id, target_estimate_id)).lastrowid


def insert_issues(conn: sqlite3.Connection, job_id: int,
                  issues: Iterable[tuple[str, str, str | None, str, str | None, str | None]]) -> None:
    """issues: (konum, önem, alan, mesaj, ham değer, çözüm)"""
    conn.executemany(
        "INSERT INTO import_issues(import_job_id, location, severity, field, message, raw_value, resolution)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", ((job_id, *i) for i in issues))


def list_jobs(conn: sqlite3.Connection, limit: int = 200) -> list[ImportJobRow]:
    rows = conn.execute(
        "SELECT j.*, sf.original_name,"
        " COALESCE(pb.title || ' ' || pb.year || CASE WHEN pb.edition <> '' THEN ' ' || pb.edition ELSE '' END,"
        "          e.name) AS target,"
        " (SELECT COUNT(*) FROM import_issues i WHERE i.import_job_id = j.id) AS issue_count"
        " FROM import_jobs j LEFT JOIN source_files sf ON sf.id = j.source_file_id"
        " LEFT JOIN price_books pb ON pb.id = j.target_book_id LEFT JOIN estimates e ON e.id = j.target_estimate_id"
        " ORDER BY j.id DESC LIMIT ?", (limit,))
    return [ImportJobRow(r["id"], r["kind"], r["status"], r["started_at"], r["finished_at"], r["original_name"],
                         r["target"], json.loads(r["stats_json"] or "{}"), r["issue_count"]) for r in rows]


def job_issues(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM import_issues WHERE import_job_id = ? ORDER BY id", (job_id,)).fetchall()


def save_template(conn: sqlite3.Connection, kind: str, authority_id: int | None, name: str, mapping: dict) -> None:
    conn.execute("DELETE FROM import_templates WHERE kind = ? AND IFNULL(authority_id, 0) = IFNULL(?, 0) AND name = ?",
                 (kind, authority_id, name))
    conn.execute("INSERT INTO import_templates(kind, authority_id, name, mapping_json) VALUES (?, ?, ?, ?)",
                 (kind, authority_id, name, json.dumps(mapping, ensure_ascii=False)))


def list_templates(conn: sqlite3.Connection, kind: str, authority_id: int | None) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT name, mapping_json FROM import_templates WHERE kind = ? AND (authority_id IS NULL OR authority_id = ?)"
        " ORDER BY name", (kind, authority_id))
    return [(r["name"], json.loads(r["mapping_json"])) for r in rows]
