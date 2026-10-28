"""Veritabanı yedekleme ve geri yükleme.

- Yedek, çalışan veritabanından SQLite backup API ile tutarlı biçimde alınır.
- Önce geçici dosyaya yazılır, bütünlüğü doğrulanır, sonra atomik olarak adlandırılır.
- Geri yüklemeden önce mevcut veritabanının da yedeği alınır.
- "elle" nedenli yedekler otomatik temizlemede silinmez.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yapimmaliyet.db.connection import check_integrity
from yapimmaliyet.infra.errors import BackupError, InsufficientDiskSpaceError

log = logging.getLogger(__name__)

BACKUP_PREFIX = "yedek_"
MANUAL_REASON = "elle"
_REASON_RE = re.compile(r"[^a-z0-9_]+")
_NAME_RE = re.compile(r"^yedek_(\d{8}_\d{6}_\d{6})_([a-z0-9_]+)\.db$")


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: datetime
    reason: str
    size_bytes: int


class BackupService:
    def __init__(self, backups_dir: Path, keep_count: int = 10):
        self.backups_dir = backups_dir
        self.keep_count = keep_count

    # --- yedek alma -------------------------------------------------------
    def create_backup(self, conn: sqlite3.Connection, reason: str = MANUAL_REASON) -> Path:
        reason = _REASON_RE.sub("_", reason.lower()).strip("_") or MANUAL_REASON
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self._check_disk_space(conn)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final = self.backups_dir / f"{BACKUP_PREFIX}{stamp}_{reason}.db"
        tmp = final.with_suffix(".db.tmp")
        try:
            dest = sqlite3.connect(str(tmp))
            try:
                conn.backup(dest)
                problems = check_integrity(dest, quick=False)
            finally:
                dest.close()
            if problems:
                raise BackupError("Alınan yedeğin bütünlük kontrolü başarısız oldu; yedek kaydedilmedi.",
                                  detail="; ".join(problems[:5]))
            os.replace(tmp, final)
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(
                "Yedek alınamadı. Disk dolu, bağlantı kopmuş (USB çıkarılmış) ya da "
                "Yedekler klasörü yazmaya kapalı olabilir.",
                detail=str(exc),
            ) from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    log.warning("Geçici yedek dosyası silinemedi: %s", tmp)
        log.info("Yedek alındı: %s", final.name)
        self.prune()
        return final

    def _check_disk_space(self, conn: sqlite3.Connection) -> None:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        needed = int(page_count * page_size * 1.2) + 1_000_000
        free = shutil.disk_usage(self.backups_dir).free
        if free < needed:
            raise InsufficientDiskSpaceError(
                f"Yedek için yeterli disk alanı yok (gerekli ≈ {needed // 1_000_000 + 1} MB, "
                f"boş {free // 1_000_000} MB). Eski yedekleri başka bir diske taşıyın "
                "ya da yer açın."
            )

    # --- listeleme ve temizlik -------------------------------------------
    def list_backups(self) -> list[BackupInfo]:
        if not self.backups_dir.exists():
            return []
        result = []
        for p in self.backups_dir.iterdir():
            m = _NAME_RE.match(p.name)
            if not m:
                continue
            created = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S_%f")
            result.append(BackupInfo(p, created, m.group(2), p.stat().st_size))
        result.sort(key=lambda b: b.created_at, reverse=True)
        return result

    def prune(self) -> list[Path]:
        automatic = [b for b in self.list_backups() if b.reason != MANUAL_REASON]
        removed = []
        for b in automatic[self.keep_count:]:
            try:
                b.path.unlink()
                removed.append(b.path)
            except OSError:
                log.warning("Eski yedek silinemedi: %s", b.path)
        return removed

    # --- doğrulama ve geri yükleme ----------------------------------------
    @staticmethod
    def verify_backup(path: Path) -> int:
        """Yedeği doğrular ve şema sürümünü döndürür."""
        if not path.is_file():
            raise BackupError(f"Yedek dosyası bulunamadı: {path}")
        try:
            src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                problems = check_integrity(src, quick=False)
                if problems:
                    raise BackupError("Yedek dosyası bozuk; geri yüklenemez.",
                                      detail="; ".join(problems[:5]))
                row = src.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            finally:
                src.close()
        except sqlite3.Error as exc:
            raise BackupError("Seçilen dosya bu programın geçerli bir yedeği değil.",
                              detail=str(exc)) from exc
        if not row or row[0] is None:
            raise BackupError("Seçilen dosya bu programın geçerli bir yedeği değil (şema bilgisi yok).")
        return int(row[0])

    def restore(self, conn: sqlite3.Connection, backup_path: Path) -> Path:
        """Yedeği etkin veritabanına geri yükler; önce mevcut durumun yedeğini alır.

        Döndürülen değer, geri yükleme öncesi alınan güvenlik yedeğinin yoludur.
        Çağıran taraf geri yüklemeden sonra migration'ları yeniden çalıştırmalıdır.
        """
        self.verify_backup(backup_path)
        safety = self.create_backup(conn, "geri_yukleme_oncesi")
        try:
            src = sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                src.backup(conn)
            finally:
                src.close()
        except sqlite3.Error as exc:
            raise BackupError(
                "Geri yükleme başarısız oldu. Mevcut verileriniz şu yedekte korunuyor: "
                f"{safety.name}",
                detail=str(exc),
            ) from exc
        log.info("Geri yüklendi: %s (öncesi: %s)", backup_path.name, safety.name)
        return safety
