"""YapımMaliyet AppContext singleton — Django sunucusu boyunca tek örnek."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yapimmaliyet.services.app_context import AppContext

log = logging.getLogger(__name__)
_ctx: "AppContext | None" = None


def init_context() -> "AppContext":
    """Sunucu başlatılırken bir kez çağrılır.

    Önce normal bootstrap dener. Kilit çakışırsa (masaüstü uygulama açık)
    salt okunur modda SQLite bağlantısı açar — yazma işlemleri yapılmaz
    ama tüm sorgular çalışır.
    """
    global _ctx
    if _ctx is not None:
        return _ctx
    from django.conf import settings as djconf
    from yapimmaliyet.services.app_context import bootstrap
    from yapimmaliyet.infra.errors import AlreadyRunningError

    data_dir = Path(djconf.YAPIMMALIYET_VERI)
    log.info("AppContext başlatılıyor: %s", data_dir)
    try:
        _ctx = bootstrap(data_dir)
    except AlreadyRunningError:
        log.warning("Veri dizini kilitli; salt okunur modda açılıyor: %s", data_dir)
        _ctx = _bootstrap_readonly(data_dir)
    return _ctx


def _bootstrap_readonly(data_dir: Path) -> "AppContext":
    """Kilit almadan salt okunur SQLite bağlantısı aç."""
    import sqlite3
    from yapimmaliyet.db.connection import open_database
    from yapimmaliyet.db.migrator import current_version, latest_version
    from yapimmaliyet.infra.paths import AppPaths, resolve_paths
    from yapimmaliyet.infra.settings import Settings, load_settings
    from yapimmaliyet.services.app_context import AppContext
    from yapimmaliyet.services.backup_service import BackupService
    from yapimmaliyet.infra.instance_lock import InstanceLock

    paths = resolve_paths(data_dir)
    paths.ensure()
    settings = load_settings(paths.settings_file)
    backup = BackupService(paths.backups_dir, settings.backup_keep_count)
    # Salt okunur URI bağlantısı: WAL okuyucusu olarak çalışır
    conn = sqlite3.connect(
        f"file:{paths.db_file}?mode=ro",
        uri=True, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Kilitli — geçici bir kilit nesnesi (serbest bırakılacak bir şey yok)
    lock = InstanceLock(paths.lock_file)
    ctx = AppContext(paths=paths, settings=settings, conn=conn, backup=backup, lock=lock,
                     warnings=["Salt okunur mod: masaüstü uygulama açık."])
    log.info("Salt okunur mod aktif; şema: %s", current_version(conn))
    return ctx


def get_ctx() -> "AppContext | None":
    """Her view'dan çağrılır; None döndüğünde view hata sayfası gösterir."""
    return _ctx
