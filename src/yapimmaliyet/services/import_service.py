"""İçe aktarma iş akışı: dosya inceleme → içerik çıkarımı → eşleme → ön kayıt → onaylı kayıt.

Kayıt adımı tek transaction içindedir: kayıtlar, iş kaydı ve sorun listesi birlikte yazılır
ya da hiçbiri yazılmaz. Kaynak dosyanın bir kopyası veri klasöründe saklanır (izlenebilirlik).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, imports, positions, price_books, rate_positions
from yapimmaliyet.domain.models import PRICE_DB_PLACES, BookStatus, NewPositionPrice
from yapimmaliyet.domain.money import to_db_text
from yapimmaliyet.domain.periods import PeriodGuess, detect_period
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search
from yapimmaliyet.fileio import csv_reader, excel_reader, pdf_coded_list, pdf_ocr, pdf_rates, pdf_text
from yapimmaliyet.fileio.detect import FileKind, detect_kind, sha256_file
from yapimmaliyet.fileio.mapping import ColumnMapping, ImportKind, suggest_mapping
from yapimmaliyet.fileio.number_format import NumberFormat
from yapimmaliyet.fileio.raw_table import RawTable, cell_text
from yapimmaliyet.infra.errors import AppError, ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.import_staging import (
    DescOnlyRows,
    RowStatus,
    StagingOptions,
    StagingResult,
    build,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceInfo:
    path: Path
    kind: FileKind
    sha256: str
    size_bytes: int
    previous_imports: int  # aynı dosyanın daha önce tamamlanmış içe aktarma sayısı


@dataclass
class ExtractResult:
    tables: list[RawTable]
    scanned_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    analysis_book: bool = False  # PDF bir analiz kitabı: arayüz analiz içe aktarma akışına yönlendirir
    rate_positions: list = field(default_factory=list)   # kod satırlı listedeki oran pozları (Ölçü: %)


@dataclass(frozen=True)
class CommitReport:
    job_id: int
    added: int
    excluded: int
    skipped_lines: int


class ImportService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path, ocr_threshold: Callable[[], float]):
        self.conn = conn
        self.data_dir = data_dir
        self.source_dir = data_dir / "KaynakDosyalar"
        self._ocr_threshold = ocr_threshold

    # --- dosya ve içerik ---------------------------------------------------
    def inspect(self, path: Path | str) -> SourceInfo:
        path = Path(path)
        kind = detect_kind(path)
        sha = sha256_file(path)
        src = imports.source_by_sha(self.conn, sha)
        previous = len(imports.jobs_for_source(self.conn, src["id"])) if src else 0
        return SourceInfo(path, kind, sha, path.stat().st_size, previous)

    def extract(self, info: SourceInfo) -> ExtractResult:
        if info.kind is FileKind.XLSX:
            return ExtractResult(excel_reader.read_xlsx(info.path))
        if info.kind is FileKind.CSV:
            return ExtractResult([csv_reader.read_csv(info.path)])
        pdf = pdf_text.analyze_pdf(info.path)
        result = ExtractResult([], scanned_pages=pdf.scanned_pages)
        if pdf.text_pages:
            if pdf_coded_list.is_analysis_book(info.path, pdf.text_pages):
                result.analysis_book = True
                result.warnings.append(
                    "Bu dosya bir birim fiyat ANALİZ kitabı görünüyor. Analizlerdeki bileşen satırları (işçilik, "
                    "malzeme rayiçleri) poz değildir; bu dosyayı birim fiyat kitabı olarak içe aktarmayın. "
                    "Analizleri \"Analiz Kitapları\" sayfasından içe aktarın; poz fiyatları için ilgili birim "
                    "fiyat listesini kullanın.")
            elif pdf_coded_list.looks_like_coded_list(info.path, pdf.text_pages):
                # Kod satırlı fiyat listesi (ör. ÇŞB birim fiyat kitabı): önerilen tablo olarak ilk sırada
                result.tables.append(pdf_coded_list.read_coded_list(info.path, pdf.text_pages))
                result.rate_positions = pdf_rates.read_rate_positions(info.path, pdf.text_pages)
            result.tables.append(pdf_text.read_pdf_text(info.path, pdf.text_pages))
        if pdf.scanned_pages:
            result.warnings.append(f"{len(pdf.scanned_pages)} sayfa taranmış görüntü içeriyor; "
                                   "bu sayfalar için OCR çalıştırmanız gerekir.")
        if not result.tables and not pdf.scanned_pages:
            raise ValidationError("PDF'te okunabilir içerik bulunamadı.")
        return result

    @staticmethod
    def period_guess(info: SourceInfo, extract: ExtractResult) -> PeriodGuess:
        """Belgenin ilk satırlarından ve dosya adından dönem / geçerlilik önerisi (yalnız öneri; kullanıcı onaylar)."""
        from yapimmaliyet.fileio.raw_table import cell_text

        lines = [info.path.stem.replace("_", " ").replace("-", " ")]
        for table in extract.tables[:3]:
            lines += [" ".join(cell_text(v) for v in row.values) for row in table.rows[:60]]
            lines += [f"{k} {v}" for k, v in table.meta.items()]
        return detect_period("\n".join(lines))

    @staticmethod
    def ocr_status() -> tuple[bool, str]:
        try:
            info = pdf_ocr.require_tesseract()
        except pdf_ocr.OcrUnavailableError as exc:
            return False, exc.user_message
        return True, f"OCR hazır (Tesseract: {info.exe})"

    @staticmethod
    def run_ocr(info: SourceInfo, pages: list[int], progress=None, cancelled=None) -> RawTable:
        return pdf_ocr.ocr_pdf(info.path, pages, progress=progress, cancelled=cancelled)

    # --- eşleme ------------------------------------------------------------
    @staticmethod
    def suggest(table: RawTable, kind: ImportKind) -> ColumnMapping:
        return suggest_mapping(table, kind)

    def stage(self, table: RawTable, kind: ImportKind, mapping: ColumnMapping, desc_only: DescOnlyRows,
              book_id: int | None = None) -> StagingResult:
        missing = mapping.missing_required(kind)
        if missing:
            raise ValidationError("Zorunlu alanlar için sütun seçilmedi: " + ", ".join(missing))
        existing = positions.codes_in_book(self.conn, book_id) if book_id is not None else set()
        opts = StagingOptions(mapping.number_format, desc_only, float(self._ocr_threshold()))
        return build(table, kind, mapping, opts, existing)

    def auto_stage(self, info: SourceInfo, kind: ImportKind) -> tuple[ColumnMapping, StagingResult, RawTable]:
        """Dosyayı okur, ilk uygun tabloda sütunları otomatik eşler ve önizlemeyi üretir.

        Veritabanına erişmez (arka planda çalıştırılabilir). Eşleme eksikse, sihirbazla elle eşlemeyi öneren
        hata verir. Döner: (eşleme, önizleme, okunan tablo)."""
        extracted = self.extract(info)
        if extracted.analysis_book:
            raise ValidationError(extracted.warnings[0])
        desc = DescOnlyRows.GROUP if kind is ImportKind.FIYAT_KITABI else DescOnlyRows.CONTINUATION
        missing: list[str] = []
        for table in extracted.tables:
            mapping = suggest_mapping(table, kind)
            missing = mapping.missing_required(kind)
            if not missing:
                staging = self.stage(table, kind, mapping, desc)
                staging.rate_positions = list(extracted.rate_positions)
                return mapping, staging, table
        if not extracted.tables:
            raise ValidationError("Dosyada okunabilir tablo bulunamadı" + (
                " (taranmış sayfalar için önce OCR gerekir)." if extracted.scanned_pages else "."))
        raise ValidationError(f"{info.path.name}: sütunlar otomatik tanınamadı (eksik: {', '.join(missing)}). "
                              "Dosyayı içe aktarma sihirbazıyla, sütunları elle seçerek aktarın.")

    # --- şablonlar ---------------------------------------------------------
    def save_template(self, kind: ImportKind, name: str, authority_id: int | None, table: RawTable,
                      mapping: ColumnMapping, desc_only: DescOnlyRows) -> None:
        name = name.strip()
        if not name:
            raise ValidationError("Şablon adı boş bırakılamaz.")
        header = table.rows[mapping.header_row].values if mapping.header_row is not None else []
        data = {
            "columns": {k: {"index": c, "header": normalize_search(cell_text(header[c])) if c < len(header) else ""}
                        for k, c in mapping.columns.items()},
            "number_format": mapping.number_format.value,
            "desc_only": desc_only.value,
        }
        with transaction(self.conn):
            imports.save_template(self.conn, kind.value, authority_id, name, data)

    def templates(self, kind: ImportKind, authority_id: int | None) -> list[tuple[str, dict]]:
        return imports.list_templates(self.conn, kind.value, authority_id)

    @staticmethod
    def apply_template(table: RawTable, mapping: ColumnMapping, template: dict) -> tuple[ColumnMapping, DescOnlyRows]:
        """Sütunları başlık metnine göre bulur (konumu değişmiş olsa da); bulunamazsa kayıtlı konumu kullanır."""
        header = table.rows[mapping.header_row].values if mapping.header_row is not None else []
        by_text = {normalize_search(cell_text(v)): i for i, v in enumerate(header) if cell_text(v)}
        cols = {}
        for key, spec in template.get("columns", {}).items():
            idx = by_text.get(spec.get("header") or "\x00", spec.get("index"))
            if idx is not None and idx < table.width:
                cols[key] = idx
        fmt = NumberFormat(template.get("number_format", mapping.number_format.value))
        return ColumnMapping(mapping.header_row, cols, fmt), DescOnlyRows(template.get("desc_only", "DEVAM"))

    # --- kayıt -------------------------------------------------------------
    def _store_source(self, info: SourceInfo) -> int:
        existing = imports.source_by_sha(self.conn, info.sha256)
        if existing:
            return existing["id"]
        self.source_dir.mkdir(parents=True, exist_ok=True)
        name = f"{info.sha256}{info.path.suffix.lower()}"
        dest = self.source_dir / name
        if not dest.exists():
            try:
                shutil.copy2(info.path, dest)
            except OSError as exc:
                raise AppError("Kaynak dosyanın kopyası veri klasörüne kaydedilemedi (disk dolu olabilir).",
                               detail=str(exc)) from exc
        return imports.insert_source(self.conn, info.path.name, info.sha256, f"KaynakDosyalar/{name}",
                                     info.size_bytes)

    @staticmethod
    def _issues(staging: StagingResult):
        sev = {RowStatus.HATA: "HATA", RowStatus.UYARI: "UYARI", RowStatus.ONAY_GEREKLI: "UYARI"}
        for r in staging.rows:
            if r.status is RowStatus.GECERLI and r.include and not r.edited:
                continue
            resolution = "Hariç tutuldu" if not r.include else ("Kullanıcı onayladı" if r.confirmed else
                                                                ("Kullanıcı düzeltti" if r.edited else "Kaydedildi"))
            for msg in r.messages or ["Kullanıcı tarafından hariç tutuldu."]:
                yield (r.ref, sev.get(r.status, "BILGI"), None, msg, None, resolution)
        for s in staging.skipped:
            yield (s.ref, "BILGI", None, s.reason, None, "Veri satırı olarak alınmadı")
        for msg in staging.total_checks():   # kaynakta beyan edilen toplamlarla karşılaştırma
            yield ("Kaynak toplamları", "UYARI" if msg.startswith("⚠") else "BILGI", None, msg, None, "Bilgi")

    @staticmethod
    def _stats(staging: StagingResult, added: int) -> dict:
        counts = staging.counts()
        return {"eklenen": added, "hariç": len(staging.rows) - added, "atlanan_satir": len(staging.skipped),
                **{s.label: n for s, n in counts.items()}}

    def commit_book(self, info: SourceInfo, staging: StagingResult, mapping: ColumnMapping,
                    book_id: int) -> CommitReport:
        book = price_books.get(self.conn, book_id)
        if book is None:
            raise NotFoundError("Hedef fiyat kitabı bulunamadı.")
        if book.status != BookStatus.TASLAK:
            raise ConflictError("İçe aktarma yalnızca taslak durumundaki kitaba yapılabilir.")
        problems = staging.blocking_problems()
        if problems:
            raise ConflictError("Kayıt yapılamadı:\n" + "\n".join(problems))
        with transaction(self.conn):
            job_id, added = self.write_book_rows(info, staging, mapping, book.authority_id, book_id)
        log.info("İçe aktarma tamamlandı: iş %s, %s kayıt", job_id, added)
        return CommitReport(job_id, added, len(staging.rows) - added, len(staging.skipped))

    def write_book_rows(self, info: SourceInfo, staging: StagingResult, mapping: ColumnMapping,
                        authority_id: int, book_id: int) -> tuple[int, int]:
        """Önizlemede dahil edilen satırları kitaba yazar; iş kaydı ve sorunlar dahil.

        Transaction AÇMAZ: çağıran kendi transaction'ı içinde çağırır (birden çok adımın atomik olması için).
        Döner: (iş kimliği, eklenen kayıt sayısı)."""
        rows = staging.included()
        existing = positions.codes_in_book(self.conn, book_id)
        if any(normalize_code(r.parsed["code"]) in existing for r in rows):
            raise ConflictError("Önizlemeden sonra kitaba aynı kodlu kayıtlar eklenmiş. Önizlemeyi yenileyin.")
        items = [NewPositionPrice(r.parsed["code"], r.parsed["description"], r.parsed["unit"],
                                  r.parsed["unit_price"], r.parsed.get("category"), r.page, r.line,
                                  None, r.min_confidence, r.parsed.get("montage_price")) for r in rows]
        source_id = self._store_source(info)
        job_id = imports.insert_job(self.conn, ImportKind.FIYAT_KITABI.value, source_id,
                                    _mapping_json(mapping, staging), self._stats(staging, len(items)),
                                    target_book_id=book_id)
        positions.insert_prices(self.conn, authority_id, book_id, items, import_job_id=job_id)
        rate_positions.insert_many(self.conn, book_id, staging.rate_positions, job_id)
        imports.insert_issues(self.conn, job_id, self._issues(staging))
        self.conn.execute("UPDATE price_books SET source_file_id = COALESCE(source_file_id, ?),"
                          " import_job_id = COALESCE(import_job_id, ?) WHERE id = ?", (source_id, job_id, book_id))
        audit.log(self.conn, "price_book", book_id, "TOPLU", new={"import_job_id": job_id, "added": len(items)},
                  note=f"Dosyadan içe aktarıldı: {info.path.name}")
        return job_id, len(items)

    def commit_estimate(self, info: SourceInfo, staging: StagingResult, mapping: ColumnMapping,
                        project_id: int, estimate_name: str) -> tuple[CommitReport, int]:
        """Keşif listesini projeye yeni bir keşif (revizyon) olarak kaydeder. Eşleştirme Aşama 5'tedir."""
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError("Proje bulunamadı.")
        estimate_name = estimate_name.strip()
        if not estimate_name:
            raise ValidationError("Keşif adı boş bırakılamaz.")
        problems = staging.blocking_problems()
        if problems:
            raise ConflictError("Kayıt yapılamadı:\n" + "\n".join(problems))
        rows = staging.included()
        with transaction(self.conn):
            source_id = self._store_source(info)
            rev = self.conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM estimates WHERE project_id = ?"
                                    " AND name = ?", (project_id, estimate_name)).fetchone()[0]
            est_id = self.conn.execute("INSERT INTO estimates(project_id, name, revision) VALUES (?, ?, ?)",
                                       (project_id, estimate_name, rev)).lastrowid
            job_id = imports.insert_job(self.conn, ImportKind.KESIF.value, source_id, _mapping_json(mapping, staging),
                                        self._stats(staging, len(rows)), target_estimate_id=est_id)
            self.conn.execute("UPDATE estimates SET import_job_id = ? WHERE id = ?", (job_id, est_id))
            for n, r in enumerate(rows, start=1):
                p = r.parsed
                self.conn.execute(
                    "INSERT INTO estimate_items(estimate_id, line_no, work_group, raw_code, raw_description, raw_unit,"
                    " quantity, raw_unit_price, raw_amount, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (est_id, n, p.get("work_group"), p.get("code"), p.get("description"), p.get("unit"),
                     to_db_text(p["quantity"], 3), _opt_text(p.get("unit_price")), _opt_text(p.get("amount")),
                     _join_notes(p, r.ref)))
            imports.insert_issues(self.conn, job_id, self._issues(staging))
            audit.log(self.conn, "estimate", est_id, "TOPLU", new={"import_job_id": job_id, "added": len(rows)},
                      note=f"Keşif içe aktarıldı: {info.path.name}")
        return CommitReport(job_id, len(rows), len(staging.rows) - len(rows), len(staging.skipped)), est_id

    # --- geçmiş ------------------------------------------------------------
    def jobs(self, limit: int = 200) -> list[imports.ImportJobRow]:
        return imports.list_jobs(self.conn, limit)

    def job_issues(self, job_id: int) -> list[sqlite3.Row]:
        return imports.job_issues(self.conn, job_id)


def _opt_text(value) -> str | None:
    return None if value is None else to_db_text(value, PRICE_DB_PLACES)


def _join_notes(parsed: dict, ref: str) -> str:
    parts = [f"Kaynak satır: {ref}"]
    for key, label in (("source", "Kaynak"), ("notes", "Not"), ("line_no", "Dosyadaki sıra no")):
        if parsed.get(key):
            parts.append(f"{label}: {parsed[key]}")
    return " | ".join(parts)


def _mapping_json(mapping: ColumnMapping, staging: StagingResult) -> dict:
    return {"header_row": mapping.header_row, "columns": mapping.columns,
            "number_format": mapping.number_format.value, "desc_only": staging.options.desc_only.value,
            "ocr_threshold": staging.options.ocr_threshold}
