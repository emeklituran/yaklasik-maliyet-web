"""Alan tanımları, başlık satırı tespiti ve sütun eşleme önerisi."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from yapimmaliyet.domain.text_tr import normalize_search
from yapimmaliyet.fileio.number_format import NumberFormat, detect_format
from yapimmaliyet.fileio.raw_table import RawTable, cell_text


class ImportKind(StrEnum):
    FIYAT_KITABI = "FIYAT_KITABI"
    KESIF = "KESIF"

    @property
    def label(self) -> str:
        return "Birim fiyat kitabı" if self is ImportKind.FIYAT_KITABI else "Keşif listesi"


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    required: bool
    numeric: bool = False


FIELDS: dict[ImportKind, list[FieldDef]] = {
    ImportKind.FIYAT_KITABI: [
        FieldDef("code", "Poz No", True),
        FieldDef("description", "Tanım", True),
        FieldDef("unit", "Birim", True),
        FieldDef("unit_price", "Birim Fiyat", True, numeric=True),
        FieldDef("category", "Kategori", False),
        FieldDef("montage_price", "Montaj Bedeli", False, numeric=True),
    ],
    ImportKind.KESIF: [
        FieldDef("line_no", "Sıra No", False),
        FieldDef("code", "Poz No", False),
        FieldDef("description", "İmalat Açıklaması", False),
        FieldDef("unit", "Birim", False),
        FieldDef("quantity", "Miktar", True, numeric=True),
        FieldDef("unit_price", "Birim Fiyat", False, numeric=True),
        FieldDef("amount", "Tutar", False, numeric=True),
        FieldDef("work_group", "İş Grubu", False),
        FieldDef("source", "Kaynak", False),
        FieldDef("notes", "Not", False),
    ],
}

# Normalize edilmiş başlık anahtar kelimeleri. 3 karakter ve altı anahtarlar yalnızca tam eşleşir.
KEYWORDS: dict[str, list[str]] = {
    "code": ["poz no", "poz numarasi", "poz kodu", "poz nosu", "poz", "kod", "kodu"],
    "description": ["tanim", "tanimi", "imalat", "imalatin cinsi", "yapilacak isin cinsi", "isin cinsi",
                    "isin adi", "is kalemi", "imalat aciklamasi", "aciklama", "tarif", "cinsi"],
    "unit": ["birim", "birimi", "olcu birimi", "olcu"],
    "unit_price": ["birim fiyat", "birim fiyati", "b fiyat", "b fiyati", "fiyat", "fiyati", "rayic"],
    "montage_price": ["montaj bedeli", "montaj bedeli tl"],
    "quantity": ["miktar", "miktari", "metraj"],
    "amount": ["tutar", "tutari", "toplam tutar", "toplam"],
    "line_no": ["sira no", "sira", "s no", "no"],
    "work_group": ["is grubu", "is grubu adi", "grup"],
    "category": ["kategori", "bolum", "grup"],
    "source": ["kaynak", "kurum"],
    "notes": ["not", "notlar"],
}


@dataclass
class ColumnMapping:
    header_row: int | None  # tablo içindeki satır indeksi; None: başlık yok, veri ilk satırdan başlar
    columns: dict[str, int] = field(default_factory=dict)
    number_format: NumberFormat = NumberFormat.TR

    def missing_required(self, kind: ImportKind) -> list[str]:
        return [f.label for f in FIELDS[kind] if f.required and f.key not in self.columns]


def _score(header: str, keyword: str) -> int:
    if header == keyword:
        return 3
    if len(keyword) <= 3:
        return 0
    if header.startswith(keyword + " ") or header.startswith(keyword):
        return 2
    if f" {keyword}" in f" {header}":
        return 1
    return 0


def match_header_cell(text: str, kind: ImportKind) -> tuple[str | None, int, int]:
    """(alan, puan, anahtar uzunluğu) — en uzun ve en güçlü eşleşme kazanır."""
    norm = normalize_search(text)
    if not norm:
        return None, 0, 0
    allowed = {f.key for f in FIELDS[kind]}
    best: tuple[str | None, int, int] = (None, 0, 0)
    for key in allowed:
        for kw in KEYWORDS[key]:
            s = _score(norm, kw)
            if s and (s, len(kw)) > (best[1], best[2]):
                best = (key, s, len(kw))
    return best


def map_header_row(values: list, kind: ImportKind, skip: set[int] | None = None) -> dict[str, int]:
    """Başlık satırından alan → sütun önerisi; `skip` (ör. Excel'de gizli sütunlar) otomatik eşlenmez."""
    candidates = []
    for col, v in enumerate(values):
        if isinstance(v, str) and col not in (skip or ()):
            key, score, length = match_header_cell(v, kind)
            if key:
                candidates.append((score, length, key, col))
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for score, length, key, col in sorted(candidates, reverse=True):
        if key not in mapping and col not in used:
            mapping[key] = col
            used.add(col)
    return mapping


def suggest_mapping(table: RawTable, kind: ImportKind, scan_rows: int = 40) -> ColumnMapping:
    best_row, best_map, best_score = None, {}, 0
    required = {f.key for f in FIELDS[kind] if f.required}
    for i, row in enumerate(table.rows[:scan_rows]):
        m = map_header_row(row.values, kind, table.hidden_columns)
        score = len(m) * 2 + len(required & m.keys())
        if len(m) >= 2 and score > best_score:
            best_row, best_map, best_score = i, m, score
    mapping = ColumnMapping(best_row, best_map)
    mapping.number_format = guess_number_format(table, mapping, kind) or NumberFormat.TR
    return mapping


def data_rows(table: RawTable, mapping: ColumnMapping):
    start = 0 if mapping.header_row is None else mapping.header_row + 1
    return table.rows[start:]


def numeric_samples(table: RawTable, mapping: ColumnMapping, kind: ImportKind) -> list[str]:
    cols = [mapping.columns[f.key] for f in FIELDS[kind] if f.numeric and f.key in mapping.columns]
    out = []
    for row in data_rows(table, mapping):
        for c in cols:
            v = row.cell(c)
            if isinstance(v, str):
                out.append(v)
    return out


def guess_number_format(table: RawTable, mapping: ColumnMapping, kind: ImportKind) -> NumberFormat | None:
    return detect_format(numeric_samples(table, mapping, kind)).fmt


def header_labels(table: RawTable, mapping: ColumnMapping) -> list[str]:
    """Sütun seçim listesi için 'Sütun 3: Birim Fiyat' biçiminde etiketler."""
    header = table.rows[mapping.header_row].values if mapping.header_row is not None else []
    labels = []
    for c in range(table.width):
        h = cell_text(header[c]) if c < len(header) else ""
        label = f"Sütun {c + 1}: {h}" if h else f"Sütun {c + 1}"
        labels.append(label + (" (gizli)" if c in table.hidden_columns else ""))
    return labels
