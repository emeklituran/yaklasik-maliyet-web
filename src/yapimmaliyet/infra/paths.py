"""Taşınabilir veri klasörü yolları.

Veri klasörü, çalıştırılabilir dosyanın (paketlenmiş hâlde) ya da proje
kökünün (geliştirme ortamında) yanındaki `Veri/` klasörüdür. Sabit bir
kullanıcı klasörüne bağlı değildir; USB bellekten çalıştırmaya uygundur.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from yapimmaliyet.infra.errors import DataDirNotWritableError

DATA_DIR_NAME = "Veri"
DATA_DIR_ENV = "YAPIMMALIYET_VERI"


def app_base_dir() -> Path:
    """Programın çalıştığı klasör."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/yapimmaliyet/infra/paths.py → proje kökü
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "yapimmaliyet.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "Yedekler"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "Gunluk"

    @property
    def source_files_dir(self) -> Path:
        return self.data_dir / "KaynakDosyalar"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "Disari"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "ayarlar.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / ".kilit"

    def ensure(self) -> None:
        """Klasörleri oluşturur ve yazılabilir olduğunu doğrular."""
        try:
            for d in (self.data_dir, self.backups_dir, self.logs_dir,
                      self.source_files_dir, self.exports_dir):
                d.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / f".yazma_testi_{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise DataDirNotWritableError(
                f"Veri klasörüne yazılamıyor: {self.data_dir}\n"
                "Klasör yazmaya karşı korumalı olabilir (ör. Program Files altında "
                "ya da kilitli bir USB bellekte). Programı yazılabilir bir konuma "
                "taşıyın veya Ayarlar'dan başka bir veri klasörü seçin.",
                detail=str(exc),
            ) from exc


def resolve_paths(data_dir: Path | str | None = None) -> AppPaths:
    """Öncelik: parametre → ortam değişkeni → program klasörü/Veri."""
    if data_dir is None:
        env = os.environ.get(DATA_DIR_ENV)
        data_dir = Path(env) if env else app_base_dir() / DATA_DIR_NAME
    return AppPaths(Path(data_dir).resolve())


def is_network_path(path: Path) -> bool:
    """Yol bir ağ paylaşımında mı? (SQLite WAL ağ dosya sistemlerinde güvenilir değildir.)"""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if sys.platform == "win32":
        import ctypes

        drive = Path(text).anchor
        if drive:
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOTE
    return False
