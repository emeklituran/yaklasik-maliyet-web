"""Endeks serileri ve aylık değerleri (TÜİK Yİ-ÜFE, alt endeksler).

Değerler kullanıcı tarafından girilir ya da iki sütunlu (dönem, değer) Excel/CSV dosyasından alınır;
program değer uydurmaz. Her değişiklik değişiklik kaydına yazılır.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.indexing import period_of
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.parsing import parse_user_decimal
from yapimmaliyet.fileio import csv_reader, excel_reader
from yapimmaliyet.fileio.raw_table import cell_text
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require

VALUE_PLACES = 6
_PERIOD_RE = re.compile(r"^\s*(\d{4})\s*[-./]\s*(\d{1,2})\s*$|^\s*(\d{1,2})\s*[-./]\s*(\d{4})\s*$")
_MONTHS = {"ocak": 1, "subat": 2, "şubat": 2, "mart": 3, "nisan": 4, "mayis": 5, "mayıs": 5, "haziran": 6,
           "temmuz": 7, "agustos": 8, "ağustos": 8, "eylul": 9, "eylül": 9, "ekim": 10, "kasim": 11, "kasım": 11,
           "aralik": 12, "aralık": 12}


@dataclass(frozen=True)
class Series:
    id: int
    code: str
    name: str
    source: str
    notes: str | None
    value_count: int
    last_period: str | None

    @property
    def label(self) -> str:
        return f"{self.code} — {self.name}"


@dataclass(frozen=True)
class IndexImportRow:
    period: str
    value: Decimal
    ref: str
    existing: Decimal | None


@dataclass(frozen=True)
class IndexImportPreview:
    series_id: int
    source_name: str
    rows: tuple[IndexImportRow, ...]
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def replacements(self) -> tuple[IndexImportRow, ...]:
        return tuple(r for r in self.rows if r.existing is not None and r.existing != r.value)

    @property
    def unchanged(self) -> tuple[IndexImportRow, ...]:
        return tuple(r for r in self.rows if r.existing == r.value)


def parse_period(value) -> str:
    """"2026-07", "07.2026", "2026/7", "Temmuz 2026" ya da tarih → "2026-07"."""
    if hasattr(value, "year") and hasattr(value, "month"):
        return f"{value.year:04d}-{value.month:02d}"
    text = cell_text(value).strip()
    m = _PERIOD_RE.match(text)
    if m:
        y, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    else:
        parts = text.replace(",", " ").split()
        if len(parts) == 2 and parts[0].lower() in _MONTHS and parts[1].isdigit():
            y, mo = parts[1], _MONTHS[parts[0].lower()]
        else:
            try:
                return period_of(text)
            except ValueError:
                raise ValidationError(f"Dönem anlaşılamadı: {text!r} (ör. 2026-07 ya da Temmuz 2026)") from None
    y, mo = int(y), int(mo)
    if not (1990 <= y <= 2200 and 1 <= mo <= 12):
        raise ValidationError(f"Geçersiz dönem: {text!r}")
    return f"{y:04d}-{mo:02d}"


def _value(value) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else parse_user_decimal(cell_text(value))
    except ValueError as exc:
        raise ValidationError(f"Endeks değeri sayı değil: {cell_text(value)!r}") from exc
    if d <= 0:
        raise ValidationError("Endeks değeri sıfırdan büyük olmalıdır.")
    return d


class IndexService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- seriler -----------------------------------------------------------
    def list_series(self) -> list[Series]:
        rows = self.conn.execute(
            "SELECT s.*, COUNT(v.id) AS n, MAX(v.period) AS last FROM index_series s"
            " LEFT JOIN index_values v ON v.series_id = s.id GROUP BY s.id ORDER BY s.id <> 1, s.code")
        return [Series(r["id"], r["code"], r["name"], r["source"], r["notes"], r["n"], r["last"]) for r in rows]

    def get_series(self, series_id: int) -> Series:
        s = next((x for x in self.list_series() if x.id == series_id), None)
        if s is None:
            raise NotFoundError("Endeks serisi bulunamadı.")
        return s

    def add_series(self, code: str, name: str, source: str = "TÜİK", notes: str | None = None) -> int:
        code = require(code, "Seri kodu").upper()
        name = require(name, "Seri adı")
        if self.conn.execute("SELECT 1 FROM index_series WHERE code = ?", (code,)).fetchone():
            raise ConflictError(f"'{code}' kodlu endeks serisi zaten var.")
        with transaction(self.conn):
            sid = self.conn.execute("INSERT INTO index_series(code, name, source, notes) VALUES (?, ?, ?, ?)",
                                    (code, name, clean(source) or "", clean(notes))).lastrowid
            audit.log(self.conn, "index_series", sid, "EKLE", new={"code": code, "name": name})
        return sid

    def delete_series(self, series_id: int) -> None:
        s = self.get_series(series_id)
        used = self.conn.execute("SELECT (SELECT COUNT(*) FROM estimates WHERE index_series_id = ?)"
                                 " + (SELECT COUNT(*) FROM estimate_items WHERE index_series_id = ?)",
                                 (series_id, series_id)).fetchone()[0]
        if used:
            raise ConflictError("Bu endeks serisi keşiflerde kullanılıyor; önce keşiflerden kaldırın.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM index_series WHERE id = ?", (series_id,))
            audit.log(self.conn, "index_series", series_id, "SIL", old={"code": s.code, "values": s.value_count})

    # --- değerler ----------------------------------------------------------
    def values(self, series_id: int) -> list[tuple[str, Decimal, str | None]]:
        return [(r["period"], from_db_text(r["value"]), r["source"]) for r in self.conn.execute(
            "SELECT period, value, source FROM index_values WHERE series_id = ? ORDER BY period DESC", (series_id,))]

    def set_value(self, series_id: int, period, value, source: str | None = None) -> None:
        self.set_values(series_id, [(period, value)], source)

    def set_values(self, series_id: int, rows: list[tuple], source: str | None = None) -> int:
        self.get_series(series_id)
        parsed = {}
        for period, value in rows:
            normalized = parse_period(period)
            if normalized in parsed:
                raise ValidationError(f"Dosyada aynı dönem birden fazla kez var: {normalized}.")
            parsed[normalized] = _value(value)
        with transaction(self.conn):
            self._write_values(series_id, parsed, source)
        return len(parsed)

    def _write_values(self, series_id: int, parsed: dict[str, Decimal], source: str | None) -> None:
        for period, value in parsed.items():
            old = self.conn.execute("SELECT value FROM index_values WHERE series_id = ? AND period = ?",
                                    (series_id, period)).fetchone()
            text = to_db_text(value, VALUE_PLACES)
            if old and old["value"] == text:
                continue
            self.conn.execute(
                "INSERT INTO index_values(series_id, period, value, source) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(series_id, period) DO UPDATE SET value = excluded.value, source = excluded.source,"
                " entered_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')",
                (series_id, period, text, clean(source)))
            audit.log(self.conn, "index_value", series_id, "GUNCELLE" if old else "EKLE",
                      old={"period": period, "value": old["value"]} if old else None,
                      new={"period": period, "value": text})

    def delete_value(self, series_id: int, period: str) -> None:
        old = self.conn.execute("SELECT value FROM index_values WHERE series_id = ? AND period = ?",
                                (series_id, period)).fetchone()
        if old is None:
            raise NotFoundError("Silinecek endeks değeri bulunamadı.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM index_values WHERE series_id = ? AND period = ?", (series_id, period))
            audit.log(self.conn, "index_value", series_id, "SIL", old={"period": period, "value": old["value"]})

    def preview_import_file(self, series_id: int, path: Path | str) -> IndexImportPreview:
        """Endeks dosyasını doğrular; mevcut dönemi değiştirmeden eklenecek/değişecek satırları önizler."""
        self.get_series(series_id)
        path = Path(path)
        if path.suffix.lower() == ".xlsx":
            tables = excel_reader.read_xlsx(path)
        elif path.suffix.lower() in (".csv", ".txt"):
            tables = [csv_reader.read_csv(path)]
        else:
            raise ValidationError("Endeks dosyası .xlsx ya da .csv olmalıdır.")
        candidates: list[tuple[object, list[tuple[str, Decimal, str]]]] = []
        skipped: list[str] = []
        warnings = [warning for table in tables for warning in table.warnings]
        for table in tables:
            columns = [c for c in range(table.width) if c not in table.hidden_columns]
            if len(columns) < 2:
                continue
            found: list[tuple[str, Decimal, str]] = []
            seen: set[str] = set()
            started = False
            for row in table.rows:
                if row.hidden:
                    skipped.append(f"{row.ref}: Excel'de gizli satır; içe aktarılmadı.")
                    continue
                raw_period, raw_value = row.cell(columns[0]), row.cell(columns[1])
                period_text, value_text = cell_text(raw_period).strip(), cell_text(raw_value).strip()
                if not period_text and not value_text:
                    continue
                if not period_text or not value_text:
                    if started:
                        raise ValidationError(f"{row.ref}: dönem ya da değer boş; içe aktarma iptal edildi.")
                    skipped.append(f"{row.ref}: başlık/boş satır.")
                    continue
                try:
                    period = parse_period(raw_period)
                    value = _value(raw_value)
                except ValidationError:
                    if started:
                        raise ValidationError(f"{row.ref}: dönem ya da değer okunamadı; içe aktarma iptal edildi.") \
                            from None
                    skipped.append(f"{row.ref}: başlık satırı.")
                    continue
                if period in seen:
                    raise ValidationError(f"{row.ref}: {period} dönemi dosyada birden fazla kez var.")
                seen.add(period)
                found.append((period, value, row.ref))
                started = True
            if found:
                candidates.append((table, found))
        if len(candidates) > 1:
            names = ", ".join(table.name for table, _ in candidates)
            raise ValidationError(f"Birden çok Excel sayfasında endeks verisi bulundu ({names}). "
                                  "Yalnız içe aktarılacak değerleri içeren tek sayfalı bir dosya seçin.")
        if not candidates:
            raise ValidationError("Dosyada (dönem, değer) satırı bulunamadı.")
        _table, found = candidates[0]
        current = {period: value for period, value, _source in self.values(series_id)}
        rows = tuple(IndexImportRow(period, value, ref, current.get(period)) for period, value, ref in found)
        return IndexImportPreview(series_id, path.name, rows, tuple(skipped), tuple(warnings))

    def apply_import(self, preview: IndexImportPreview, *, confirm_overwrites: bool = False) -> int:
        """Önizlenen değerleri kaydeder; mevcut farklı değerler için açık onay ve eskimemiş önizleme ister."""
        self.get_series(preview.series_id)
        replacements = preview.replacements
        if replacements and not confirm_overwrites:
            periods = ", ".join(row.period for row in replacements[:5])
            raise ConflictError(f"{len(replacements)} mevcut dönem farklı değerle değişecek ({periods}). "
                                "Değişiklik için açık onay gerekli.")
        with transaction(self.conn):
            current = {period: value for period, value, _source in self.values(preview.series_id)}
            for row in preview.rows:
                if current.get(row.period) != row.existing:
                    raise ConflictError(f"{row.period} dönemi önizlemeden sonra değişti. Dosyayı yeniden önizleyin.")
            values = {row.period: _value(row.value) for row in preview.rows}
            self._write_values(preview.series_id, values, f"Dosya: {preview.source_name}")
        return len(values)

    def import_file(self, series_id: int, path: Path | str, *, confirm_overwrites: bool = False) -> int:
        """Uyumluluk kolaylığı: önizler, yalnızca farklı mevcut değerler açıkça onaylandıysa uygular."""
        preview = self.preview_import_file(series_id, path)
        return self.apply_import(preview, confirm_overwrites=confirm_overwrites)

    def lookup(self):
        """Hesap motoru için (seri kodu, dönem) → değer işlevi."""
        cache: dict[tuple[str, str], Decimal | None] = {}

        def get(code: str, period: str) -> Decimal | None:
            key = (code, period)
            if key not in cache:
                r = self.conn.execute("SELECT v.value FROM index_values v JOIN index_series s ON s.id = v.series_id"
                                      " WHERE s.code = ? AND v.period = ?", key).fetchone()
                cache[key] = from_db_text(r["value"]) if r else None
            return cache[key]
        return get
