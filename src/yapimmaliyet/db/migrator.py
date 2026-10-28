"""Sürümlü şema migration sistemi.

- Migration'lar `db/migrations/NNNN_ad.sql` dosyalarıdır, sırayla uygulanır.
- Her migration tek transaction içinde çalışır; hata olursa hiçbir değişiklik kalmaz.
- Uygulanmış bir migration dosyası sonradan değiştirilirse (checksum farkı) çalışma durdurulur.
- Mevcut verisi olan bir veritabanında migration'dan önce `before_migrate` çağrılır (yedek için).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.infra.errors import MigrationError, SchemaTooNewError

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_builtin_migrations() -> list[Migration]:
    result = []
    for entry in resources.files("yapimmaliyet.db.migrations").iterdir():
        m = _NAME_RE.match(entry.name)
        if m:
            result.append(Migration(int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    result.sort(key=lambda x: x.version)
    for expected, mig in enumerate(result, start=1):
        if mig.version != expected:
            raise MigrationError("Migration dosyaları sıralı değil (eksik numara var).",
                                 detail=f"beklenen {expected}, bulunan {mig.version}")
    return result


def latest_version(migrations: Sequence[Migration] | None = None) -> int:
    migs = load_builtin_migrations() if migrations is None else migrations
    return migs[-1].version if migs else 0


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')))"
    )


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def _split_statements(sql: str) -> list[str]:
    """Betiği ifadelere böler; trigger gövdelerindeki ';' doğru ele alınır."""
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise MigrationError("Migration betiği tamamlanmamış bir ifade içeriyor.", detail=buf[:200])
    return statements


def migrate(conn: sqlite3.Connection,
            migrations: Sequence[Migration] | None = None,
            before_migrate: Callable[[int, int], None] | None = None) -> int:
    """Bekleyen migration'ları uygular, yeni şema sürümünü döndürür."""
    migs = list(load_builtin_migrations() if migrations is None else migrations)
    _ensure_table(conn)
    applied = {r["version"]: r["checksum"]
               for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    known = {m.version: m for m in migs}

    newest_known = migs[-1].version if migs else 0
    if applied and max(applied) > newest_known:
        raise SchemaTooNewError(
            "Bu veritabanı programın daha yeni bir sürümüyle oluşturulmuş. "
            "Verilerinizin zarar görmemesi için programın güncel sürümünü kullanın.",
            detail=f"veritabanı şeması {max(applied)}, program en fazla {newest_known}",
        )
    for version, checksum in applied.items():
        if version not in known or known[version].checksum != checksum:
            raise MigrationError(
                "Veritabanı şema kayıtları program dosyalarıyla uyuşmuyor. "
                "Program dosyaları değiştirilmiş olabilir; çalışma güvenlik için durduruldu.",
                detail=f"migration {version} checksum farklı",
            )

    pending = [m for m in migs if m.version not in applied]
    if not pending:
        return current_version(conn)

    start = current_version(conn)
    if start > 0 and before_migrate is not None:
        before_migrate(start, pending[-1].version)

    for mig in pending:
        log.info("Migration uygulanıyor: %04d_%s", mig.version, mig.name)
        try:
            with transaction(conn):
                for stmt in _split_statements(mig.sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                    (mig.version, mig.name, mig.checksum),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"Veritabanı şeması güncellenemedi (adım {mig.version}). "
                "Hiçbir değişiklik yapılmadı; varsa güncelleme öncesi yedek Yedekler klasöründedir.",
                detail=str(exc),
            ) from exc
    return current_version(conn)
