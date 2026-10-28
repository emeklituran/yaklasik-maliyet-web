"""Kullanıcı ayarları (Veri/ayarlar.json).

Dosya atomik olarak yazılır. Bozuk dosya, kenara alınarak varsayılanlarla
devam edilir; böylece program açılamaz hâle gelmez.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Settings:
    company_name: str = ""
    contact_name: str = ""
    setup_completed: bool = False
    backup_keep_count: int = 10
    backup_on_exit: bool = True
    ocr_confidence_threshold: int = 75
    show_tour: bool = True                     # açılışta eğitim turu (tamamlanınca ya da işaretlenince kapanır)
    show_toolbar: bool = False                 # Görünüm → Araç Çubuğu (kurumsal düzende kapalı)
    hidden_modules: list[str] = field(default_factory=list)   # kenar menüde gizlenen modül başlıkları
    layout_rev: int = 0                        # arayüz düzeni sürümü (bir kerelik geçişler için)
    shortcuts: dict[str, str] = field(default_factory=dict)   # komut kimliği → kısayol (varsayılandan farklı olanlar)

    def validate(self) -> None:
        if not all(isinstance(v, str) and len(v) <= 200 for v in (self.company_name, self.contact_name)):
            raise ValueError("Firma ve sorumlu bilgisi en fazla 200 karakter olmalıdır.")
        if not isinstance(self.setup_completed, bool) or not isinstance(self.backup_on_exit, bool):
            raise ValueError("Kurulum ve yedekleme tercihleri evet/hayır olmalıdır.")
        if not 1 <= self.backup_keep_count <= 500:
            raise ValueError("Saklanacak yedek sayısı 1 ile 500 arasında olmalıdır.")
        if not 0 <= self.ocr_confidence_threshold <= 100:
            raise ValueError("OCR güven eşiği 0 ile 100 arasında olmalıdır.")
        if not isinstance(self.show_tour, bool) or not isinstance(self.show_toolbar, bool):
            raise ValueError("Görünüm ayarları evet/hayır olmalıdır.")
        if not isinstance(self.hidden_modules, list) or not all(isinstance(v, str) for v in self.hidden_modules):
            raise ValueError("Modül seçimi geçersiz.")
        if not isinstance(self.layout_rev, int) or self.layout_rev < 0:
            raise ValueError("Arayüz düzeni sürümü geçersiz.")
        if not isinstance(self.shortcuts, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in self.shortcuts.items()):
            raise ValueError("Kısayol ayarları geçersiz.")


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        settings = Settings(**{k: v for k, v in raw.items() if k in known})
        settings.validate()
        return settings
    except (ValueError, TypeError, OSError) as exc:
        broken = path.with_suffix(".bozuk.json")
        log.warning("Ayar dosyası okunamadı, varsayılanlar kullanılıyor: %s", exc)
        try:
            os.replace(path, broken)
        except OSError:
            log.exception("Bozuk ayar dosyası kenara alınamadı")
        return Settings()


def save_settings(path: Path, settings: Settings) -> None:
    settings.validate()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
