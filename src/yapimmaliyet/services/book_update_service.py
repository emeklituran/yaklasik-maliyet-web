"""Yeni fiyat listesiyle kütüphane güncellemesi: önizleme (fark), onaylı uygulama ve geri alma.

Fiyat güncelleme politikası (mevcut kitap/sürüm mimarisine uygun):
- Eski kitabın kayıtları HİÇBİR ZAMAN değiştirilmez ya da silinmez. Güncelleme her zaman YENİ bir kitap
  (idare + yıl + sürüm) oluşturur; aynı pozun eski ve yeni fiyatı ayrı kitaplarda, kaynağıyla birlikte durur.
- Hangi fiyatın kullanılacağını keşifteki kaynak önceliği belirler (yeni kitap öne alınır).
- BİRLEŞTİR kipinde yeni listede bulunmayan pozlar baz kitaptan (eski fiyatıyla) yeni kitaba taşınır;
  TAM kipinde yeni kitap yalnızca dosyadaki kayıtlardan oluşur.
- Birimi değişen pozlar otomatik alınmaz: kullanıcı tek tek kabul etmezse yeni kitaba dosyadaki hâli yazılmaz
  (BİRLEŞTİR kipinde baz kitaptaki hâli taşınır). Tanım farkı yalnızca işaretlenir.
- Bütün adımlar tek transaction'dır; yarıda kalan işlem veritabanını değiştirmez.
- Geri alma: güncellemeyle oluşturulan kitap (henüz hiçbir keşif ya da analizde kullanılmıyorsa) silinir,
  baz kitap arşive alındıysa yeniden aktifleştirilir.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, positions, price_books, rate_positions
from yapimmaliyet.domain.models import BookStatus
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search, normalize_unit
from yapimmaliyet.fileio.mapping import ColumnMapping, ImportKind
from yapimmaliyet.infra.errors import ConflictError, NotFoundError
from yapimmaliyet.services.import_service import ImportService, SourceInfo
from yapimmaliyet.services.import_staging import RowStatus, StagingResult
from yapimmaliyet.services.library_service import LibraryService


class Change(StrEnum):
    YENI = "YENI"
    FIYAT = "FIYAT"
    AYNI = "AYNI"
    BIRIM = "BIRIM"

    @property
    def label(self) -> str:
        return {"YENI": "Yeni poz", "FIYAT": "Fiyatı değişen", "AYNI": "Aynı kalan",
                "BIRIM": "Birim çelişkisi"}[self.value]


class Mode(StrEnum):
    BIRLESTIR = "BIRLESTIR"   # yeni liste + listede olmayan pozlar baz kitaptan
    TAM = "TAM"               # yalnızca yeni liste


@dataclass(frozen=True)
class DiffRow:
    code: str
    code_norm: str
    description: str
    unit: str
    new_price: Decimal
    change: Change
    ref: str                          # kaynak dosyadaki konum (sayfa/satır)
    old_price: Decimal | None = None
    old_unit: str | None = None
    old_description: str | None = None
    desc_changed: bool = False
    montage_changed: bool = False

    @property
    def change_ratio(self) -> Decimal | None:
        if self.old_price in (None, Decimal(0)) or self.change is Change.YENI:
            return None
        return self.new_price / self.old_price - 1


@dataclass(frozen=True)
class MissingRow:
    code: str
    description: str
    unit: str
    price: Decimal


@dataclass
class UpdatePreview:
    base_book_id: int
    base_signature: tuple[int, int]
    rows: list[DiffRow]
    missing_in_file: list[MissingRow]
    excluded: list[tuple[str, str]] = field(default_factory=list)   # (konum, neden) — dosyada hatalı/hariç satırlar

    def counts(self) -> dict[Change, int]:
        out = {c: 0 for c in Change}
        for r in self.rows:
            out[r.change] += 1
        return out

    def by_change(self, change: Change) -> list[DiffRow]:
        return [r for r in self.rows if r.change is change]


@dataclass(frozen=True)
class UpdateResult:
    new_book_id: int
    base_book_id: int
    job_id: int
    from_file: int
    carried_over: int
    unit_conflicts_skipped: int
    base_archived: bool


def _signature(conn: sqlite3.Connection, book_id: int) -> tuple[int, int]:
    r = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM position_prices WHERE price_book_id = ?",
                     (book_id,)).fetchone()
    return int(r[0]), int(r[1])


class BookUpdateService:
    def __init__(self, conn: sqlite3.Connection, importer: ImportService):
        self.conn = conn
        self.importer = importer
        self.library = LibraryService(conn)

    # --- önizleme ----------------------------------------------------------
    def preview(self, staging: StagingResult, base_book_id: int) -> UpdatePreview:
        """Dosyanın önizlemesini (staging) baz kitapla karşılaştırır; veritabanına yazmaz."""
        self.library.get_book(base_book_id)
        base = price_books.prices_by_code(self.conn, base_book_id)
        rows, seen, excluded = [], set(), []
        for r in staging.rows:
            if r.status is RowStatus.HATA or not r.include:
                excluded.append((r.ref, "; ".join(r.messages) or "Kullanıcı hariç tuttu"))
                continue
            p = r.parsed
            key = normalize_code(p["code"])
            seen.add(key)
            old = base.get(key)
            if old is None:
                rows.append(DiffRow(p["code"], key, p["description"], p["unit"], p["unit_price"], Change.YENI, r.ref))
                continue
            old_price = from_db_text(old["unit_price"])
            if normalize_unit(p["unit"]) != old["unit_norm"]:
                change = Change.BIRIM
            else:
                change = Change.AYNI if p["unit_price"] == old_price else Change.FIYAT
            old_montage = from_db_text(old["montage_price"]) if old["montage_price"] is not None else None
            rows.append(DiffRow(
                p["code"], key, p["description"], p["unit"], p["unit_price"], change, r.ref, old_price, old["unit"],
                old["description"], normalize_search(p["description"]) != normalize_search(old["description"]),
                p.get("montage_price") is not None and p.get("montage_price") != old_montage))
        missing = [MissingRow(o["code"], o["description"], o["unit"], from_db_text(o["unit_price"]))
                   for k, o in sorted(base.items()) if k not in seen]
        return UpdatePreview(base_book_id, _signature(self.conn, base_book_id), rows, missing, excluded)

    # --- uygulama ----------------------------------------------------------
    def apply(self, info: SourceInfo, staging: StagingResult, mapping: ColumnMapping, preview: UpdatePreview, *,
              title: str, year: int, edition: str | None, valid_from: str | None = None,
              mode: Mode = Mode.BIRLESTIR, accept_unit_changes: set[str] | frozenset[str] = frozenset(),
              archive_base: bool = False) -> UpdateResult:
        base = self.library.get_book(preview.base_book_id)
        if _signature(self.conn, base.id) != preview.base_signature:
            raise ConflictError("Baz kitap önizlemeden sonra değişmiş. Önizlemeyi yenileyin.")
        problems = staging.blocking_problems()
        if problems:
            raise ConflictError("Güncelleme yapılamadı:\n" + "\n".join(problems))
        title, year, edition, valid_from = self.library.validate_new_book(base.authority_id, title, year, edition,
                                                                          valid_from)
        skip = {r.code_norm for r in preview.by_change(Change.BIRIM)} - set(accept_unit_changes)
        touched = [r for r in staging.rows if r.include and normalize_code(r.parsed.get("code") or "") in skip]
        for r in touched:     # kabul edilmeyen birim çelişkileri bu işlemde yazılmaz
            r.include = False
        try:
            with transaction(self.conn):
                note = (f"{base.display_name} kitabının güncellemesi ({mode.value}); kaynak: {info.path.name}")
                new_id = price_books.insert(self.conn, base.authority_id, title, year, edition, valid_from, note)
                job_id, added = self.importer.write_book_rows(info, staging, mapping, base.authority_id, new_id)
                carried = price_books.copy_prices(self.conn, base.id, new_id, only_missing=True) \
                    if mode is Mode.BIRLESTIR else 0
                if mode is Mode.BIRLESTIR:   # listede olmayan oran pozları da baz kitaptan taşınır
                    rate_positions.copy(self.conn, base.id, new_id)
                archived = archive_base and base.status is BookStatus.AKTIF
                if archived:
                    price_books.set_status(self.conn, base.id, BookStatus.ARSIV)
                audit.log(self.conn, "price_book", new_id, "TOPLU",
                          new={"base_book_id": base.id, "mode": mode.value, "from_file": added, "carried_over": carried,
                               "unit_conflicts_skipped": len(skip), "base_archived": archived, "job_id": job_id},
                          note="Fiyat kitabı güncellemesi")
        finally:
            for r in touched:
                r.include = True
        return UpdateResult(new_id, base.id, job_id, added, carried, len(skip), archived)

    # --- geri alma ---------------------------------------------------------
    def undo(self, result: UpdateResult) -> None:
        book = self.library.get_book(result.new_book_id)
        if price_books.reference_count(self.conn, book.id):
            raise ConflictError("Güncellemeyle oluşturulan kitabın fiyatları keşif ya da analizlerde kullanılıyor; "
                                "geri alınamaz. Önce o eşleşmeleri kaldırın.")
        with transaction(self.conn):
            price_books.delete_with_prices(self.conn, book.id)
            positions.delete_orphan_positions(self.conn)
            if result.base_archived:
                base = price_books.get(self.conn, result.base_book_id)
                if base is None:
                    raise NotFoundError("Baz kitap bulunamadı.")
                if base.status is BookStatus.ARSIV:
                    price_books.set_status(self.conn, base.id, BookStatus.AKTIF)
            audit.log(self.conn, "price_book", book.id, "SIL",
                      old={"title": book.title, "year": book.year, "edition": book.edition,
                           "price_count": book.price_count, "base_book_id": result.base_book_id},
                      note="Fiyat kitabı güncellemesi geri alındı")


def stage_price_list(importer: ImportService, info: SourceInfo):
    """Fiyat listesini okur, sütunları otomatik eşler ve önizlemeyi üretir. Veritabanına erişmez
    (arka plan iş parçacığında çalıştırılabilir). Otomatik eşleme eksikse ne yapılacağını söyleyen hata verir."""
    return importer.auto_stage(info, ImportKind.FIYAT_KITABI)


def default_edition(existing: list[str], hint: str) -> str:
    """Çakışmayan bir sürüm adı önerir (ör. 'Ekim', 'Ekim (2)')."""
    hint = hint.strip() or "Güncelleme"
    if hint not in existing:
        return hint
    n = 2
    while f"{hint} ({n})" in existing:
        n += 1
    return f"{hint} ({n})"
