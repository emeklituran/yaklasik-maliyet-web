"""Kurumsal kimlik (neuvatech): logo, simge, antet ve kurumsal renk (ui/assets).

Dosya bulunamazsa None döner: ekranlar ve çıktılar logosuz çalışır."""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "ui" / "assets"
LOGO = ASSETS / "logo.png"        # yazı logo (saydam): açılış ekranı
SYMBOL = ASSETS / "symbol.svg"    # simge (üç eğik çubuk): açılış ekranı
LETTERHEAD = ASSETS / "antet.png" # simge + yazı logo yan yana: PDF/Excel anteti
ICON = ASSETS / "icon.png"        # kare simge: pencere ve görev çubuğu
ICO = ASSETS / "app.ico"          # exe dosya simgesi (paketleme)
CORPORATE = "#C90000"             # kurumsal renk


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None


def logo_path() -> Path | None:
    return _existing(LOGO)


def symbol_path() -> Path | None:
    return _existing(SYMBOL)


def letterhead_path() -> Path | None:
    return _existing(LETTERHEAD) or logo_path()


def icon_path() -> Path | None:
    return _existing(ICON)
