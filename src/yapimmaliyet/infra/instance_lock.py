"""Aynı veri klasörünün iki program tarafından aynı anda açılmasını engeller.

İşletim sistemi kilidi kullanılır; program beklenmedik biçimde kapansa da
kilit kendiliğinden kalkar (eski kilit dosyası sorun yaratmaz).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO

from yapimmaliyet.infra.errors import AlreadyRunningError


class InstanceLock:
    def __init__(self, lock_file: Path):
        self.lock_file = lock_file
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        fh = open(self.lock_file, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunningError(
                "Bu veri klasörü başka bir program penceresinde açık. "
                "Diğer pencereyi kapatıp tekrar deneyin.",
                detail=str(exc),
            ) from exc
        fh.truncate(0)
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
