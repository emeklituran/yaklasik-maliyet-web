"""Resmî birim fiyat ANALİZ kitaplarının içe aktarılması (birim fiyat listesinden ayrı akış).

Akış: dosya → önizleme (ayrıştırma + aritmetik denetim + mükerrer kod tespiti) → onaylı kayıt.
Kayıt tek transaction içindedir; yalnızca analysis_books / official_analyses / official_analysis_items,
source_files ve audit_log tablolarına yazılır. Poz/fiyat verisine (positions, position_prices,
price_books) hiçbir şey yazılmaz; karşılaştırma yalnızca okumadır.

Not: import_jobs.kind CHECK kısıtı yalnızca 'FIYAT_KITABI' ve 'KESIF' değerlerine izin verdiğinden
analiz içe aktarmaları için import_jobs kaydı yazılmaz (rapor `job_note` alanında belirtir).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, authorities, imports
from yapimmaliyet.db.repositories import official_analyses as repo
from yapimmaliyet.domain.models import PRICE_DB_PLACES
from yapimmaliyet.domain.money import to_db_text
from yapimmaliyet.domain.periods import MONTHS
from yapimmaliyet.domain.text_tr import normalize_code, tr_lower
from yapimmaliyet.fileio import pdf_coded_list
from yapimmaliyet.fileio.detect import sha256_file
from yapimmaliyet.fileio.pdf_analysis import ParsedAnalysis, parse_analysis_book
from yapimmaliyet.fileio.pdf_text import open_pdf
from yapimmaliyet.infra.errors import AppError, ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require

log = logging.getLogger(__name__)

QTY_DB_PLACES = 10    # kesirli miktarlar (21/4,50) için; kaynaktaki yazım ayrıca saklanır
RATE_DB_PLACES = 4
JOB_NOTE = ("İçe aktarma işleri (import_jobs) tablosundaki tür kısıtı analiz kitabına izin vermediği için "
            "iş kaydı yazılmadı; izlenebilirlik kaynak dosya kaydı ve değişiklik günlüğüyle sağlandı.")


@dataclass
class AnalysisPreview:
    path: Path
    sha256: str
    size_bytes: int
    page_count: int
    pages: list[int] | None
    analyses: list[ParsedAnalysis]
    duplicates: dict[str, list[int]] = field(default_factory=dict)   # code_norm → sayfalar
    warnings: list[str] = field(default_factory=list)
    previous_books: list[str] = field(default_factory=list)          # aynı dosyadan önce oluşturulan kitaplar

    @property
    def total(self) -> int:
        return len(self.analyses)

    @property
    def consistent(self) -> int:
        return sum(1 for a in self.analyses if a.check_ok)

    @property
    def with_warnings(self) -> int:
        return self.total - self.consistent

    @property
    def missing_code(self) -> int:
        return sum(1 for a in self.analyses if not a.code)


@dataclass(frozen=True)
class AnalysisCommitReport:
    book_id: int
    source_file_id: int
    added: int
    consistent: int
    with_warnings: int
    skipped_duplicates: list[str]
    skipped_no_code: int
    import_job_id: int | None = None
    job_note: str = JOB_NOTE


class AnalysisImportService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path):
        self.conn = conn
        self.data_dir = data_dir
        self.source_dir = data_dir / "KaynakDosyalar"

    # --- önizleme ------------------------------------------------------------
    def preview(self, path: Path | str, pages: list[int] | None = None,
                progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> AnalysisPreview:
        return self.add_previous(self.parse(path, pages, progress, cancelled))

    def parse(self, path: Path | str, pages: list[int] | None = None,
              progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> AnalysisPreview:
        """Veritabanına dokunmaz: arka plan iş parçacığında çalıştırılabilir. Ardından `add_previous`."""
        path = Path(path)
        if not path.is_file():
            raise ValidationError(f"Dosya bulunamadı: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValidationError("Analiz kitabı içe aktarma yalnızca PDF dosyalarını destekler.")
        page_count, is_analysis = _inspect(path, pages)
        if not is_analysis:
            raise ValidationError("Bu dosya bir birim fiyat ANALİZ kitabı görünmüyor (\"Analizin Adı\", \"Malzeme "
                                  "Bileşenleri\" gibi başlıklar bulunamadı). Birim fiyat listeleri için fiyat kitabı "
                                  "içe aktarmayı kullanın.")
        analyses = parse_analysis_book(path, pages, progress=progress, cancelled=cancelled)
        if not analyses:
            raise ValidationError("Dosyada okunabilir analiz bulunamadı. Taranmış bir belge olabilir.")
        sha = sha256_file(path)
        prev = self._preview_summary(analyses)
        return AnalysisPreview(path, sha, path.stat().st_size, page_count, list(pages) if pages else None,
                               analyses, prev[0], prev[1], [])

    def add_previous(self, result: AnalysisPreview) -> AnalysisPreview:
        """Dosyanın daha önce analiz kitabı olarak içe aktarılıp aktarılmadığını önizlemeye ekler."""
        src = imports.source_by_sha(self.conn, result.sha256)
        previous = [f"{b.title} {b.year}" + (f" {b.edition}" if b.edition else "")
                    for b in (repo.books_for_source(self.conn, src["id"]) if src else [])]
        result.previous_books[:] = previous
        if previous:
            result.warnings.insert(0, "Bu dosya daha önce analiz kitabı olarak içe aktarılmış: " + ", ".join(previous))
        return result

    @staticmethod
    def _preview_summary(analyses: list[ParsedAnalysis]) -> tuple[dict[str, list[int]], list[str]]:
        seen: dict[str, list[int]] = {}
        for a in analyses:
            if a.code:
                seen.setdefault(normalize_code(a.code), []).append(a.page)
        dups = {k: v for k, v in seen.items() if len(v) > 1}
        ok = sum(1 for a in analyses if a.check_ok)
        warnings = [f"{len(analyses)} analiz okundu: {ok} tanesi aritmetik denetimden geçti, "
                    f"{len(analyses) - ok} tanesinde uyarı var (değerler düzeltilmedi; kaynakla karşılaştırın)."]
        if dups:
            warnings.append(f"Kitapta {len(dups)} poz kodu birden fazla kez geçiyor (ör. {next(iter(dups))}); "
                            "kayıtta ilk analiz alınır, diğerleri atlanır.")
        missing = sum(1 for a in analyses if not a.code)
        if missing:
            warnings.append(f"{missing} analizin poz numarası okunamadı; bunlar kaydedilmez.")
        return dups, warnings

    # --- kayıt -----------------------------------------------------------------
    def commit(self, path: Path | str, preview: AnalysisPreview, authority_id: int, title: str, year: int,
               edition: str = "") -> AnalysisCommitReport:
        path = Path(path)
        title = require(title, "Kitap adı")
        edition = clean(edition) or ""
        if isinstance(year, bool) or not isinstance(year, int) or not 1950 <= year <= 2200:
            raise ValidationError("Yıl 1950 ile 2200 arasında bir tam sayı olmalıdır.")
        if authorities.get(self.conn, authority_id) is None:
            raise NotFoundError("İdare bulunamadı.")
        if not path.is_file() or sha256_file(path) != preview.sha256:
            raise ConflictError("Dosya önizlemeden sonra değişmiş ya da taşınmış. Önizlemeyi yenileyin.")
        if repo.book_exists(self.conn, authority_id, year, edition, title):
            raise ConflictError("Bu idare için aynı ad, yıl ve baskıda bir analiz kitabı zaten kayıtlı.")
        src = imports.source_by_sha(self.conn, preview.sha256)
        if src and repo.books_for_source(self.conn, src["id"]):
            raise ConflictError("Bu dosya daha önce analiz kitabı olarak içe aktarılmış; mükerrer kayıt yapılmadı.")
        rows, skipped_dup, no_code = self._rows(preview.analyses)
        with transaction(self.conn):
            source_id = self._store_source(path, preview)
            book_id = repo.insert_book(self.conn, authority_id, title, year, edition, source_id)
            dates = Counter(v["analysis_date"] for v, _ in rows if v.get("analysis_date"))
            if dates:   # dönem: analiz sayfalarında basılı tarih (en sık geçen); belgede yoksa boş kalır
                valid = dates.most_common(1)[0][0]
                self.conn.execute("UPDATE analysis_books SET valid_from = ?, period = ? WHERE id = ?",
                                  (valid, f"{valid[:4]} {MONTHS[int(valid[5:7]) - 1]}", book_id))
            for values, items in rows:
                aid = repo.insert_analysis(self.conn, book_id, values)
                repo.insert_items(self.conn, aid, items)
            consistent = sum(1 for v, _ in rows if v["check_ok"])
            audit.log(self.conn, "analysis_book", book_id, "TOPLU",
                      new={"analiz": len(rows), "tutarli": consistent, "uyarili": len(rows) - consistent,
                           "atlanan_mukerrer": len(skipped_dup), "kodsuz": no_code, "source_file_id": source_id},
                      note=f"Analiz kitabı içe aktarıldı: {path.name}")
        log.info("Analiz kitabı içe aktarıldı: kitap %s, %s analiz", book_id, len(rows))
        return AnalysisCommitReport(book_id, source_id, len(rows), consistent, len(rows) - consistent,
                                    skipped_dup, no_code)

    @staticmethod
    def _rows(analyses: list[ParsedAnalysis]) -> tuple[list[tuple[dict, list[tuple]]], list[str], int]:
        """Kayda hazır satırlar. Negatif özet değeri gibi şemaya aykırı veri varsa hiçbir şey yazılmaz."""
        rows, seen, dups, no_code, problems = [], set(), [], 0, []
        for a in analyses:
            if not a.code:
                no_code += 1
                continue
            norm = normalize_code(a.code)
            if norm in seen:
                dups.append(a.code)
                continue
            seen.add(norm)
            heads = {"subtotal": a.subtotal, "material_total": a.material_total, "labour_total": a.labour_total,
                     "overhead_amount": a.overhead_amount, "unit_price": a.unit_price,
                     "montage_price": a.montage_price}
            if any(v is not None and v < 0 for v in [*heads.values(), a.overhead_rate]):
                problems.append(f"{a.code} (s.{a.page}): negatif özet değeri")
                continue
            values = {k: _dec(v, PRICE_DB_PLACES) for k, v in heads.items()}
            values.update(code=a.code, code_norm=norm, name=a.name, unit=a.unit, analysis_date=a.date,
                          group_label=a.group_label, overhead_rate=_dec(a.overhead_rate, RATE_DB_PLACES),
                          notes=a.notes, page=a.page or None, check_ok=int(a.check_ok),
                          warnings_json=json.dumps(a.warnings, ensure_ascii=False))
            items = [(it.line_no, it.section, it.code, it.description, it.unit, _dec(it.quantity, QTY_DB_PLACES),
                      it.quantity_text, _dec(it.unit_price, PRICE_DB_PLACES), _dec(it.amount, PRICE_DB_PLACES),
                      it.column) for it in a.items]
            rows.append((values, items))
        if problems:
            raise ValidationError("Kayıt yapılamadı; şu analizlerde geçersiz değer var:\n" + "\n".join(problems[:20]))
        return rows, dups, no_code

    def _store_source(self, path: Path, preview: AnalysisPreview) -> int:
        """Kaynak dosyanın kopyası KaynakDosyalar/ altına sha256 adıyla (ImportService._store_source deseni)."""
        existing = imports.source_by_sha(self.conn, preview.sha256)
        if existing:
            return existing["id"]
        self.source_dir.mkdir(parents=True, exist_ok=True)
        name = f"{preview.sha256}{path.suffix.lower()}"
        dest = self.source_dir / name
        if not dest.exists():
            try:
                shutil.copy2(path, dest)
            except OSError as exc:
                raise AppError("Kaynak dosyanın kopyası veri klasörüne kaydedilemedi (disk dolu olabilir).",
                               detail=str(exc)) from exc
        return imports.insert_source(self.conn, path.name, preview.sha256, f"KaynakDosyalar/{name}",
                                     preview.size_bytes)

    # --- okuma -----------------------------------------------------------------
    def books(self) -> list[repo.AnalysisBookRow]:
        return repo.list_books(self.conn)

    def analyses(self, book_id: int, query: str | None = None) -> list[repo.AnalysisRow]:
        if repo.get_book(self.conn, book_id) is None:
            raise NotFoundError("Analiz kitabı bulunamadı.")
        rows = repo.list_analyses(self.conn, book_id)
        q = clean(query)
        if not q:
            return rows
        # Türkçe büyük/küçük harf (İ/ı) SQLite LIKE ile doğru eşleşmediği için süzme Python'da yapılır
        code_q, text_q = normalize_code(q), tr_lower(q)
        return [r for r in rows if r.code_norm.startswith(code_q) or text_q in tr_lower(r.name)]

    def analysis(self, analysis_id: int) -> repo.AnalysisRow:
        row = repo.get_analysis(self.conn, analysis_id)
        if row is None:
            raise NotFoundError("Analiz bulunamadı.")
        return row

    def compare_with_prices(self, book_id: int, same_authority: bool = True) -> list[repo.PriceComparison]:
        """Analiz birim fiyatı ↔ kütüphanedeki aynı kodlu poz fiyatları (fark = analiz − kitap). Yalnızca okur."""
        if repo.get_book(self.conn, book_id) is None:
            raise NotFoundError("Analiz kitabı bulunamadı.")
        return repo.compare_with_prices(self.conn, book_id, same_authority)


def _inspect(path: Path, pages: list[int] | None) -> tuple[int, bool]:
    """(sayfa sayısı, analiz kitabı mı). pdf_coded_list.is_analysis_book ile aynı işaretler kullanılır;
    ayrıca bölünmez boşluklar ( ) normalleştirilir (bazı PDF üreticileri sözcük arasına   koyar)."""
    doc = open_pdf(path)
    try:
        selected = [p for p in (pages or range(1, doc.page_count + 1)) if 1 <= p <= doc.page_count]
        if not selected:
            raise ValidationError("Seçilen sayfa aralığı dosyada yok.")
        sample = selected[:: max(1, len(selected) // 20)][:20]
        hits = sum(1 for no in sample if any(m in doc[no - 1].get_text().replace(" ", " ").lower()
                                             for m in pdf_coded_list.ANALYSIS_MARKERS))
        return doc.page_count, hits >= len(sample) / 2
    finally:
        doc.close()


def _dec(value: Decimal | None, places: int) -> str | None:
    return None if value is None else to_db_text(value, places)
