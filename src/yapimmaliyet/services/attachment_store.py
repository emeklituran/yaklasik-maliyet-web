"""Ek dosya saklama: kullanıcının seçtiği belge (PDF, tarama) veri klasörüne kopyalanır; içe aktarılmaz.

Aynı içerik tek kez saklanır (özetin ilk 12 hanesi dosya adının önüne konur). Veritabanında veri klasörüne göre
göreli yol tutulur; böylece taşınabilir klasör başka sürücüye taşınsa da dosyaya ulaşılır.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from yapimmaliyet.infra.errors import ValidationError


def store(data_dir: Path | None, subdir: str, src: Path | str) -> str:
    if data_dir is None:
        raise ValidationError("Veri klasörü tanımlı değil; dosya saklanamadı.")
    src = Path(src)
    if not src.is_file():
        raise ValidationError(f"Dosya bulunamadı: {src}")
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    safe = re.sub(r"[^\w.\- ]+", "_", src.name, flags=re.UNICODE).strip() or "dosya"
    rel = f"KaynakDosyalar/{subdir}/{h.hexdigest()[:12]}_{safe}"
    dest = Path(data_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    return rel


def resolve(data_dir: Path | None, rel: str | None) -> Path | None:
    """Saklanan dosyanın tam yolu; yoksa ya da bulunamıyorsa None."""
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute() and data_dir is not None:
        p = Path(data_dir) / rel
    return p if p.is_file() else None
