"""Dönen (rotating) günlük dosyası; her satırda uygulama sürümü bulunur."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from yapimmaliyet import __version__

LOG_FILE_NAME = "uygulama.log"
_FORMAT = f"%(asctime)s v{__version__} %(levelname)s %(name)s: %(message)s"


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> Path:
    log_file = logs_dir / LOG_FILE_NAME
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_yapimmaliyet", False):
            root.removeHandler(h)
            h.close()
    handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._yapimmaliyet = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return log_file
