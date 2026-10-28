"""Dosya biçiminin içerikten (uzantıdan değil) belirlenmesi."""

from __future__ import annotations

import hashlib
import zipfile
from enum import StrEnum
from pathlib import Path

from yapimmaliyet.infra.errors import ValidationError


class FileKind(StrEnum):
    XLSX = "XLSX"
    CSV = "CSV"
    PDF = "PDF"

    @property
    def label(self) -> str:
        return {"XLSX": "Excel (.xlsx)", "CSV": "CSV / metin", "PDF": "PDF"}[self.value]


def detect_kind(path: Path) -> FileKind:
    if not path.is_file():
        raise ValidationError(f"Dosya bulunamadı: {path}")
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        raise ValidationError("Dosya okunamadı. Başka bir programda açık olabilir.", detail=str(exc)) from exc
    if not head:
        raise ValidationError("Dosya boş.")
    if head.startswith(b"%PDF"):
        return FileKind.PDF
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                if "xl/workbook.xml" in zf.namelist():
                    return FileKind.XLSX
        except zipfile.BadZipFile as exc:
            raise ValidationError("Dosya bozuk görünüyor (geçersiz ZIP/Excel yapısı).") from exc
        raise ValidationError("Bu bir Excel dosyası değil (ör. Word ya da başka bir arşiv dosyası).")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        if _ole_encrypted(path):
            raise ValidationError("Excel dosyası parola korumalı (şifreli). Excel'de parolayı kaldırıp "
                                  "kaydedin ve yeniden deneyin.")
        raise ValidationError(
            "Eski Excel biçimi (.xls) desteklenmiyor. Dosyayı Excel'de açıp "
            "'Farklı Kaydet → Excel Çalışma Kitabı (.xlsx)' ile kaydedin ve yeniden deneyin."
        )
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" not in head:
        return FileKind.CSV
    raise ValidationError("Dosya biçimi tanınamadı. Desteklenen biçimler: .xlsx, .csv/.txt, .pdf")


def _ole_encrypted(path: Path) -> bool:
    """Parola korumalı .xlsx dosyaları OLE kabında "EncryptedPackage" akışı taşır (eski .xls'ten ayırmak için)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 20)
    except OSError:
        return False
    return any(name.encode("utf-16-le") in data for name in ("EncryptedPackage", "EncryptionInfo"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
