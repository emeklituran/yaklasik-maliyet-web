"""Programı sıfırlama: tüm verileri silme ve fabrika ayarlarına dönme.

Yalnız programın kendi bilinen dosyaları silinir (veritabanı, kaynak/ek dosyalar, dışa aktarılanlar, günlükler; fabrika
ayarında ayar ve başlangıç dosyaları). Veri klasörünün kendisi ve içindeki başka dosyalar silinmez. Silmeden önce her
zaman bir güvenlik yedeği alınır ve (kullanıcı ayrıca "yedekleri de sil" demedikçe) Yedekler klasörü korunur.
Sıfırlamadan sonra program yeniden açıldığında boş veritabanı yeniden kurulur (fabrika ayarında ilk kurulum sihirbazı gelir).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from yapimmaliyet.db.connection import close_database
from yapimmaliyet.services.app_context import AppContext
from yapimmaliyet.services.startup_service import config_path

log = logging.getLogger(__name__)

CONFIRM_WORD = "SIFIRLA"


@dataclass
class ResetReport:
    safety_backup: Path | None
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def reset_program(ctx: AppContext, factory: bool = False, delete_backups: bool = False,
                  startup_config: Path | None = None) -> ResetReport:
    """Verileri siler (factory=True: ayarlar ve başlangıç tercihleri de). Bağlantıyı ve kilidi kapatır; çağıran programı
    kapatmalı ya da yeniden başlatmalıdır."""
    p = ctx.paths
    safety = None
    try:
        safety = ctx.backup.create_backup(ctx.conn, "sifirlama_oncesi")   # geri dönüş yolu: Yedekler'den geri yüklenir
    except Exception:
        log.exception("Sıfırlama öncesi yedek alınamadı")
        raise
    ctx.settings = ctx.settings.__class__(**{**ctx.settings.__dict__, "backup_on_exit": False}) \
        if hasattr(ctx.settings, "__dict__") else ctx.settings
    close_database(ctx.conn)
    ctx.lock.release()
    for h in list(logging.getLogger().handlers):      # günlük dosyası açıkken silinemez
        name = getattr(h, "baseFilename", None)
        if name and Path(name).is_relative_to(p.logs_dir):
            h.close()
            logging.getLogger().removeHandler(h)
    report = ResetReport(safety)
    targets: list[Path] = [p.db_file, p.db_file.with_name(p.db_file.name + "-wal"), p.db_file.with_name(p.db_file.name + "-shm"),
                           p.source_files_dir, p.exports_dir, p.logs_dir, p.data_dir / ".piyasa_onbellek.json"]
    if delete_backups:   # eski yedekler silinir; az önce alınan güvenlik yedeği her zaman kalır
        targets += [f for f in p.backups_dir.glob("*") if f != safety]
    if factory:
        targets += [p.settings_file, startup_config or config_path()]
    for t in targets:
        try:
            if t.is_dir():
                shutil.rmtree(t)
            elif t.exists():
                t.unlink()
            else:
                continue
            report.deleted.append(str(t))
        except OSError as exc:
            report.errors.append(f"{t}: {exc}")
            log.exception("Sıfırlamada silinemedi: %s", t)
    return report
