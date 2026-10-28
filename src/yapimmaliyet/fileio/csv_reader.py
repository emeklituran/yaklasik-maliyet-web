"""CSV / metin okuyucu: kodlama ve ayraç tespiti.

Sayılar metin olarak kalır; biçim (1.234,56 / 1,234.56) sütun bazında ayrıca belirlenir.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from yapimmaliyet.fileio.raw_table import RawRow, RawTable, SourceKind
from yapimmaliyet.infra.errors import ValidationError

DELIMITERS = (";", ",", "\t", "|")
DELIMITER_LABELS = {";": "noktalı virgül", ",": "virgül", "\t": "sekme", "|": "dikey çizgi"}


def decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "UTF-16"
    try:
        return raw.decode("utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        # Windows-1254 her baytı çözer; Türkçe Excel'in "CSV" çıktısı genellikle budur.
        return raw.decode("cp1254"), "Windows-1254 (Türkçe)"


def sniff_delimiter(text: str) -> str:
    sample = [ln for ln in text.splitlines() if ln.strip()][:60]
    best, best_score = ";", (-1, -1.0)
    for d in DELIMITERS:
        counts = [len(r) for r in csv.reader(sample, delimiter=d)]
        if not counts:
            continue
        common, freq = Counter(counts).most_common(1)[0]
        if common < 2:
            continue
        score = (freq, common)  # önce tutarlılık, sonra sütun sayısı
        if score > best_score:
            best, best_score = d, score
    return best


def read_csv(path: Path, delimiter: str | None = None) -> RawTable:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationError("CSV dosyası okunamadı.", detail=str(exc)) from exc
    text, encoding = decode(raw)
    d = delimiter or sniff_delimiter(text)
    rows = []
    for i, rec in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=d), start=1):
        values = [c.strip() or None for c in rec]
        while values and values[-1] is None:
            values.pop()
        if values:
            rows.append(RawRow(values, f"satır {i}", line=i))
    if not rows:
        raise ValidationError("CSV dosyasında veri bulunamadı.")
    table = RawTable(path.name, rows, SourceKind.CSV,
                     meta={"Kodlama": encoding, "Ayraç": DELIMITER_LABELS.get(d, d)})
    if encoding.startswith("Windows"):
        table.warnings.append("Dosya UTF-8 değil; Windows-1254 (Türkçe) kodlamasıyla okundu. "
                              "Türkçe karakterleri önizlemede kontrol edin.")
    return table
