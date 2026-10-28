"""Uygulamanın açılış ve kapanış akışı.

Arayüzden bağımsızdır; testlerde ve ileride komut satırı araçlarında da kullanılır.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import check_integrity, close_database, open_database
from yapimmaliyet.db.migrator import current_version, latest_version, migrate
from yapimmaliyet.infra.errors import DatabaseCorruptError, SchemaTooNewError
from yapimmaliyet.infra.instance_lock import InstanceLock
from yapimmaliyet.infra.logging_setup import setup_logging
from yapimmaliyet.infra.paths import AppPaths, is_network_path, resolve_paths
from yapimmaliyet.infra.settings import Settings, load_settings
from yapimmaliyet.services.backup_service import BackupService
from yapimmaliyet.services.public_library import PublicLibraryService, seed_enabled
from yapimmaliyet.services.party_service import link_existing

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    paths: AppPaths
    settings: Settings
    conn: sqlite3.Connection
    backup: BackupService
    lock: InstanceLock
    warnings: list[str] = field(default_factory=list)

    @property
    def schema_version(self) -> int:
        return current_version(self.conn)


def bootstrap(data_dir: Path | str | None = None) -> AppContext:
    paths = resolve_paths(data_dir)
    paths.ensure()
    lock = InstanceLock(paths.lock_file)
    lock.acquire()
    try:
        setup_logging(paths.logs_dir)
        log.info("Program başlatılıyor; veri klasörü: %s", paths.data_dir)
        settings = load_settings(paths.settings_file)
        backup = BackupService(paths.backups_dir, settings.backup_keep_count)

        warnings: list[str] = []
        if is_network_path(paths.data_dir):
            warnings.append("Veri klasörü bir ağ sürücüsünde. Veri güvenliği için programı "
                            "yerel diskte ya da USB bellekte çalıştırmanız önerilir.")

        existed = paths.db_file.exists()
        conn = open_database(paths.db_file)
        try:
            if existed:
                problems = check_integrity(conn, quick=True)
                if problems:
                    raise DatabaseCorruptError(
                        "Veritabanı bütünlük kontrolünden geçemedi. Verilerinizin zarar "
                        "görmemesi için program durduruldu. Yedekler klasöründeki son "
                        "yedeği geri yükleyebilirsiniz.",
                        detail="; ".join(problems[:5]),
                    )
            version = migrate(
                conn,
                before_migrate=lambda old, new: backup.create_backup(conn, f"migration_{old}_{new}"),
            )
            linked = link_existing(conn)   # belgelerdeki firma adlarını ortak taraf kaydına bağlar
            if linked:
                log.info("Firma kaydına bağlanan belge satırı: %s", linked)
            if seed_enabled():
                try:   # kamu kütüphanesi kataloğu ve doğrulanmış başlangıç kayıtları (bir kez; açılışı engellemez)
                    if PublicLibraryService(conn).ensure_seed():
                        log.info("Kamu birim fiyat kütüphanesi başlangıç kayıtları kuruldu.")
                except Exception:
                    log.exception("Kamu kütüphanesi başlangıç kayıtları kurulamadı")
                    warnings.append("Kamu birim fiyat kütüphanesinin başlangıç kayıtları kurulamadı; ayrıntı günlükte.")
        except BaseException:
            conn.close()
            raise
        log.info("Hazır: uygulama %s, şema %s", __version__, version)
        return AppContext(paths, settings, conn, backup, lock, warnings)
    except BaseException:
        lock.release()
        raise


def restore_backup(ctx: AppContext, backup_path: Path) -> Path:
    """Yedeği geri yükler ve şemayı güncel sürüme getirir. Güvenlik yedeğinin yolunu döndürür."""
    backup_version = ctx.backup.verify_backup(backup_path)
    if backup_version > latest_version():
        raise SchemaTooNewError(
            "Bu yedek programın daha yeni bir sürümüyle alınmış; bu sürümle geri yüklenemez."
        )
    safety = ctx.backup.restore(ctx.conn, backup_path)
    migrate(ctx.conn)
    link_existing(ctx.conn)
    return safety


def shutdown(ctx: AppContext) -> None:
    try:
        if ctx.settings.backup_on_exit:
            try:
                ctx.backup.create_backup(ctx.conn, "kapanis")
            except Exception:
                log.exception("Kapanış yedeği alınamadı")
        close_database(ctx.conn)
        log.info("Program kapatıldı")
    finally:
        ctx.lock.release()
