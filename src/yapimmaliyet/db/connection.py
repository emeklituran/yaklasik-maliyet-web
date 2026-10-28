"""SQLite bağlantısı ve transaction yönetimi.

Bağlantı autocommit modunda açılır; yazma işlemleri yalnızca `transaction()`
bağlamında yapılır. Hata olursa tamamı geri alınır, yarım kayıt kalmaz.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from yapimmaliyet.infra.errors import DatabaseCorruptError

log = logging.getLogger(__name__)


def open_database(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DatabaseCorruptError(
            "Veritabanı dosyası açılamadı; dosya bozuk ya da bir SQLite veritabanı değil. "
            "Veri Yönetimi bölümünden bir yedeği geri yükleyebilirsiniz.",
            detail=str(exc),
        ) from exc
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Yazma kilidini hemen alan transaction; hata hâlinde ROLLBACK."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def check_integrity(conn: sqlite3.Connection, *, quick: bool = True) -> list[str]:
    """Sorun yoksa boş liste döner."""
    pragma = "quick_check" if quick else "integrity_check"
    try:
        rows = [r[0] for r in conn.execute(f"PRAGMA {pragma}")]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    problems = [r for r in rows if r != "ok"]
    problems += [f"Yabancı anahtar ihlali: tablo={r[0]} satır={r[1]} hedef={r[2]}" for r in fk]
    return problems


def close_database(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        log.exception("Kapanışta WAL checkpoint yapılamadı")
    finally:
        conn.close()
