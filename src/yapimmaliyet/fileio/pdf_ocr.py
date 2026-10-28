"""Taranmış PDF'ler için çevrimdışı OCR (Tesseract).

- tesseract.exe doğrudan çağrılır (pytesseract bağımlılığı gerekmez); TSV çıktısından
  kelime koordinatları ve güven değerleri alınır.
- OCR sonucu kesin kabul edilmez: hücre güven değerleri satırlara taşınır ve düşük
  güvenli alanlar kullanıcı onayı olmadan kaydedilemez.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yapimmaliyet.fileio.pdf_layout import PageWords, Word, pages_to_rows
from yapimmaliyet.fileio.pdf_text import open_pdf
from yapimmaliyet.fileio.raw_table import RawTable, SourceKind
from yapimmaliyet.infra.errors import AppError
from yapimmaliyet.infra.paths import app_base_dir

OCR_LANG = "tur"
DEFAULT_DPI = 300
TESSERACT_ENV = "YAPIMMALIYET_TESSERACT"


class OcrUnavailableError(AppError):
    pass


class OcrCancelled(AppError):
    pass


@dataclass(frozen=True)
class TesseractInfo:
    exe: Path
    tessdata: Path | None
    languages: tuple[str, ...]


def _exe_candidates() -> list[Path]:
    base = app_base_dir()
    cands = []
    if os.environ.get(TESSERACT_ENV):
        cands.append(Path(os.environ[TESSERACT_ENV]))
    cands += [base / "_internal" / "tesseract" / "tesseract.exe",
              base / "packaging" / "third_party" / "tesseract" / "tesseract.exe"]
    found = shutil.which("tesseract")
    if found:
        cands.append(Path(found))
    cands += [Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"]
    return cands


def _tessdata_candidates(exe: Path) -> list[Path]:
    base = app_base_dir()
    return [base / "_internal" / "tessdata", base / "packaging" / "third_party" / "tessdata", exe.parent / "tessdata"]


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)


def find_tesseract() -> TesseractInfo | None:
    for exe in _exe_candidates():
        if not exe.is_file():
            continue
        tessdata = next((d for d in _tessdata_candidates(exe) if (d / f"{OCR_LANG}.traineddata").is_file()), None)
        args = [str(exe), "--list-langs"] + (["--tessdata-dir", str(tessdata)] if tessdata else [])
        try:
            out = _run(args, 30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (out.stdout + out.stderr).decode("utf-8", "replace")
        langs = tuple(ln.strip() for ln in text.splitlines()[1:] if ln.strip())
        return TesseractInfo(exe, tessdata, langs)
    return None


def require_tesseract() -> TesseractInfo:
    info = find_tesseract()
    if info is None:
        raise OcrUnavailableError("OCR motoru (Tesseract) bulunamadı. Taranmış PDF'leri okumak için program "
                                  "paketindeki 'tesseract' klasörünün eksiksiz olduğundan emin olun.")
    if OCR_LANG not in info.languages:
        raise OcrUnavailableError("Tesseract bulundu ancak Türkçe dil verisi (tur.traineddata) eksik.")
    return info


def parse_tsv(tsv: str, scale: float) -> list[Word]:
    """Tesseract TSV çıktısını kelimelere çevirir; piksel koordinatları `scale` ile PDF noktasına dönüştürülür."""
    words = []
    for rec in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if rec.get("level") != "5":
            continue
        text = (rec.get("text") or "").strip()
        try:
            conf = float(rec["conf"])
            left, top = float(rec["left"]), float(rec["top"])
            width, height = float(rec["width"]), float(rec["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if not text or conf < 0:
            continue
        words.append(Word(left * scale, top * scale, (left + width) * scale, (top + height) * scale, text, conf))
    return words


def ocr_pdf(path: Path, pages: list[int], dpi: int = DEFAULT_DPI,
            progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> RawTable:
    info = require_tesseract()
    doc = open_pdf(path)
    page_words: list[PageWords] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ym_ocr_") as tmp:
            for n, no in enumerate(pages, start=1):
                if cancelled and cancelled():
                    raise OcrCancelled("OCR işlemi kullanıcı tarafından iptal edildi.")
                page = doc[no - 1]
                img = Path(tmp) / f"s{no}.png"
                page.get_pixmap(dpi=dpi).save(str(img))
                # "tsv" yapılandırma dosyası yerine parametre: ayrı tessdata klasöründe configs/ bulunmayabilir
                args = [str(info.exe), str(img), "stdout", "-l", OCR_LANG, "--psm", "6",
                        "-c", "tessedit_create_tsv=1"]
                if info.tessdata:
                    args += ["--tessdata-dir", str(info.tessdata)]
                try:
                    out = _run(args, 300)
                except subprocess.TimeoutExpired as exc:
                    raise AppError(f"OCR {no}. sayfada zaman aşımına uğradı.") from exc
                if out.returncode != 0:
                    raise AppError(f"OCR {no}. sayfada başarısız oldu.",
                                   detail=out.stderr.decode("utf-8", "replace")[:500])
                words = parse_tsv(out.stdout.decode("utf-8", "replace"), 72.0 / dpi)
                page_words.append(PageWords(no, page.rect.width, words))
                if progress:
                    progress(n, len(pages))
    finally:
        doc.close()
    rows = pages_to_rows(page_words)
    if not rows:
        raise AppError("OCR sonucunda okunabilir metin bulunamadı. Tarama kalitesini kontrol edin.")
    table = RawTable(f"{path.name} (OCR)", rows, SourceKind.PDF_OCR)
    table.warnings.append("Bu tablo OCR ile okundu; sonuçlar hatalı olabilir. Düşük güvenli hücreler "
                          "işaretlenir ve onayınız olmadan kaydedilmez.")
    return table
