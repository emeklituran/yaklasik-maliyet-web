"""YapımMaliyet — Entegre İnşaat Teknik Ofis ve Maliyet Yönetimi."""

__version__ = "0.3.10"
IS_PRERELEASE = any(tag in __version__ for tag in ("rc", "a", "b", "dev"))

try:   # paketleme sırasında üretilir (commit kimliği ve tarih); kaynaktan çalışırken yoktur
    from yapimmaliyet._build_info import BUILD_ID
except ImportError:
    BUILD_ID = "kaynak koddan çalışıyor"
    import json
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        try:
            info = json.loads((Path(sys._MEIPASS) / "build_info.json").read_text(encoding="utf-8"))
            BUILD_ID = f"{info['commit'][:12]} · {info['built_at']}"
        except (OSError, ValueError, KeyError):
            BUILD_ID = "yapı bilgisi bulunamadı"
APP_NAME = "YapımMaliyet"
APP_SUBTITLE = "Entegre İnşaat Teknik Ofis ve Maliyet Yönetimi"
APP_AUTHOR = "TURAN EMEKLİ"
APP_CREDIT = f"{APP_AUTHOR} — {APP_NAME} Yazılımı"
COPYRIGHT = f"© 2026 {APP_AUTHOR}. Tüm hakları saklıdır."
