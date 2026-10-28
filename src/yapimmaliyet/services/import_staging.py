"""İçe aktarma ön kaydı (staging): eşlenmiş ham satırların doğrulanması.

Veritabanına yazmaz; kullanıcıya önizleme ve hata listesi üretir. Kurallar:
- Zorunlu alanı eksik ya da sayısı geçersiz satır HATA'dır ve kaydedilmez.
- Yalnızca tanım içeren satırlar seçime göre önceki satırın devamı ya da grup/kategori başlığıdır.
- Kod + tanım olup birim/fiyat/miktar içermeyen satırlar başlık kabul edilir.
- OCR güveni eşiğin altındaki hücreler ONAY_GEREKLİ'dir; kullanıcı onaylamadan kaydedilmez.
- Kullanıcı bir hücreyi düzeltirse bütün satırlar yeniden doğrulanır (mükerrer kontrolü dahil).
- Excel'de gizli satır işaretlenir ve varsayılan olarak hariç tutulur; kullanıcı isterse dahil eder.
- Sayfa başında tekrar eden tablo başlığı ve "Sayfa 1 / 3" altbilgisi veri sayılmaz (atlanan satır olarak raporlanır).
- Keşifte miktarı olmayan "ara toplam / genel toplam / KDV" satırları veri sayılmaz; tutarları kaynakta beyan
  edilen toplam olarak saklanır ve aktarılacak kalemlerin toplamıyla karşılaştırılır (fark gösterilir, düzeltilmez).
- Sayı biçimi sütunlardan belirlenemiyorsa ("1.500" hem 1500 hem 1,5 olabilir) ilgili satırlar onay ister.
- Kayıtta yuvarlanacak değerler (keşifte 4 basamaktan fazla ondalık) uyarıyla gösterilir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from yapimmaliyet.domain.models import PRICE_DB_PLACES, QUANTITY_PLACES
from yapimmaliyet.domain.money import decimal_places
from yapimmaliyet.domain.rounding import line_amount
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search
from yapimmaliyet.fileio.mapping import FIELDS, ColumnMapping, ImportKind, data_rows
from yapimmaliyet.fileio.number_format import NumberFormat, classify, detect_format, parse_number
from yapimmaliyet.fileio.raw_table import RawTable


TOTAL_RE = re.compile(r"^(ara toplami?|genel toplami?|toplami?|yekunu?|nakli yekunu?|sayfa toplami?|kdv|katma deger)\b")
PAGE_RE = re.compile(r"^(sayfa\s*)?\d+\s*(/|-|of|\.)?\s*\d*\s*(sayfa)?$")
TOL = Decimal("0.01")


class RowStatus(StrEnum):
    GECERLI = "GECERLI"
    UYARI = "UYARI"
    ONAY_GEREKLI = "ONAY_GEREKLI"
    HATA = "HATA"

    @property
    def label(self) -> str:
        return {"GECERLI": "Geçerli", "UYARI": "Uyarı", "ONAY_GEREKLI": "Onay gerekli", "HATA": "Hata"}[self.value]


class DescOnlyRows(StrEnum):
    CONTINUATION = "DEVAM"  # önceki satırın tanımına eklenir (PDF'lerde sık)
    GROUP = "GRUP"          # grup / kategori başlığı kabul edilir

    @property
    def label(self) -> str:
        return "Önceki satırın devamı" if self is DescOnlyRows.CONTINUATION else "Grup / kategori başlığı"


@dataclass
class StagingOptions:
    number_format: NumberFormat = NumberFormat.TR
    desc_only: DescOnlyRows = DescOnlyRows.CONTINUATION
    ocr_threshold: float = 75.0


@dataclass
class StagedRow:
    ref: str
    page: int | None
    line: int | None
    raw: dict[str, str | Decimal | None]       # kullanıcı düzeltmeleri buraya yazılır
    confidences: dict[str, float | None]
    context: str | None = None                   # devralınan kategori / iş grubu
    parsed: dict[str, object] = field(default_factory=dict)
    status: RowStatus = RowStatus.GECERLI
    messages: list[str] = field(default_factory=list)
    low_conf_fields: list[str] = field(default_factory=list)
    include: bool = True
    confirmed: bool = False
    edited: bool = False
    hidden: bool = False                              # Excel'de gizli satır
    notes: list[str] = field(default_factory=list)   # her doğrulamada yeniden eklenen kalıcı uyarılar

    @property
    def min_confidence(self) -> float | None:
        vals = [c for c in self.confidences.values() if c is not None]
        return min(vals) if vals else None


@dataclass
class SkippedLine:
    ref: str
    reason: str


@dataclass(frozen=True)
class DeclaredTotal:
    """Kaynakta beyan edilen toplam satırı (ara toplam, KDV, genel toplam…)."""
    ref: str
    label: str
    amount: Decimal | None
    raw: str | None


@dataclass
class StagingResult:
    kind: ImportKind
    rows: list[StagedRow]
    skipped: list[SkippedLine]
    options: StagingOptions
    existing_codes: set[str] = field(default_factory=set)
    rate_positions: list = field(default_factory=list)   # kitaptaki oran pozları (fiyat değil; ayrı saklanır)
    declared_totals: list[DeclaredTotal] = field(default_factory=list)
    format_ambiguous: bool = False

    def included_total(self) -> Decimal:
        """Aktarılacak keşif kalemlerinin tutar toplamı (tutar yoksa round(miktar × birim fiyat, 2))."""
        total = Decimal(0)
        for r in self.included():
            amount, qty, price = (r.parsed.get(k) for k in ("amount", "quantity", "unit_price"))
            if isinstance(amount, Decimal):
                total += amount
            elif isinstance(qty, Decimal) and isinstance(price, Decimal):
                total += line_amount(qty, price)
        return total

    def total_checks(self) -> list[str]:
        """Kaynakta beyan edilen toplamlar ve aktarılacak kalemlerle karşılaştırma.

        KDV satırı varsa kalemler KDV'den önceki son toplamla karşılaştırılır ve KDV'den sonraki genel toplamın
        "toplam + KDV" olduğu ayrıca denetlenir; KDV satırı yoksa son genel toplam (yoksa son toplam) kullanılır.
        Farklar düzeltilmez, "⚠" ile gösterilir."""
        if not self.declared_totals:
            return []
        totals = self.declared_totals
        out = [f"Kaynakta {t.label}: {t.amount if t.amount is not None else t.raw} ({t.ref})." for t in totals]
        is_kdv = [("kdv" in normalize_search(t.label) or "katma deger" in normalize_search(t.label)) for t in totals]
        kdv_at = next((i for i, k in enumerate(is_kdv) if k), None)
        known = [i for i, t in enumerate(totals) if t.amount is not None and not is_kdv[i]]
        if kdv_at is not None:
            before = [i for i in known if i < kdv_at]
            after = [i for i in known if i > kdv_at]
            target = totals[before[-1]] if before else None
            if target is not None and after:
                kdv = sum((t.amount for t, k in zip(totals, is_kdv) if k and t.amount is not None), Decimal(0))
                gross = totals[after[-1]]
                if abs(target.amount + kdv - gross.amount) <= TOL:
                    out.append(f"Kaynakta {gross.label} = {target.label} + KDV (uyumlu).")
                else:
                    out.append(f"⚠ Kaynakta {gross.label} {gross.amount}, {target.label} + KDV = {target.amount + kdv};"
                               f" fark {gross.amount - target.amount - kdv}.")
        else:
            labels = [(i, normalize_search(totals[i].label)) for i in known]
            pick = [i for i, lb in labels if lb.startswith("genel toplam")] or [i for i, _ in labels]
            target = totals[pick[-1]] if pick else None
        if target is not None:
            computed = self.included_total()
            diff = computed - target.amount
            if abs(diff) <= TOL:
                out.append(f"Aktarılacak kalemlerin toplamı {computed} = kaynaktaki {target.label} (uyumlu).")
            else:
                out.append(f"⚠ Aktarılacak kalemlerin toplamı {computed}, kaynaktaki {target.label} {target.amount}; "
                           f"fark {diff}. Eksik ya da hariç tutulan satırları kontrol edin.")
        return out

    def total_mismatch(self) -> bool:
        return any(m.startswith("⚠") for m in self.total_checks())

    def counts(self) -> dict[RowStatus, int]:
        out = {s: 0 for s in RowStatus}
        for r in self.rows:
            out[r.status] += 1
        return out

    def included(self) -> list[StagedRow]:
        return [r for r in self.rows if r.include]

    def blocking_problems(self) -> list[str]:
        problems = []
        inc = self.included()
        if not inc:
            problems.append("Kaydedilecek satır yok.")
        bad = [r.ref for r in inc if r.status == RowStatus.HATA]
        if bad:
            problems.append(f"{len(bad)} hatalı satır dahil edilmiş (ör. {bad[0]}). Düzeltin ya da hariç tutun.")
        unconfirmed = [r.ref for r in inc if r.status == RowStatus.ONAY_GEREKLI and not r.confirmed]
        if unconfirmed:
            problems.append(f"{len(unconfirmed)} onay bekleyen satır (düşük OCR güveni ya da belirsiz sayı biçimi) "
                            f"onaylanmadı (ör. {unconfirmed[0]}).")
        return problems


def _text(v: str | Decimal | None) -> str | None:
    if v is None:
        return None
    s = format(v, "f") if isinstance(v, Decimal) else str(v).strip()
    return s or None


def _number(v: str | Decimal | None, fmt: NumberFormat) -> Decimal | None:
    if v is None or isinstance(v, Decimal):
        return v
    s = v.strip()
    return parse_number(s, fmt) if s else None


def build(table: RawTable, kind: ImportKind, mapping: ColumnMapping, options: StagingOptions,
          existing_codes: set[str] | None = None) -> StagingResult:
    """Yapısal geçiş: başlık/devam satırlarını ayırır, veri satırlarını oluşturur ve doğrular."""
    numeric = {f.key for f in FIELDS[kind] if f.numeric}
    rows: list[StagedRow] = []
    skipped: list[SkippedLine] = []
    totals: list[DeclaredTotal] = []
    context: str | None = None
    header = table.rows[mapping.header_row] if mapping.header_row is not None else None
    header_norm = {k: normalize_search(_text(header.cell(c)) or "") for k, c in mapping.columns.items()} \
        if header else {}
    for raw in data_rows(table, mapping):
        values = {k: raw.cell(c) for k, c in mapping.columns.items()}
        for k in ("work_group", "category"):     # dikey birleştirilmiş grup hücresi alt satırlara taşınır
            c = mapping.columns.get(k)
            if c is not None and values.get(k) is None and raw.merged and c in raw.merged:
                values[k] = raw.merged[c]
        conf = {k: raw.confidence(c) for k, c in mapping.columns.items()}
        texts = {k: _text(v) for k, v in values.items()}
        if not any(texts.values()):
            continue
        same = [k for k, t in texts.items() if t and header_norm.get(k) and normalize_search(t) == header_norm[k]]
        if len(same) >= 2:
            skipped.append(SkippedLine(raw.ref, "Tekrar eden tablo başlığı (sayfa başı)"))
            continue
        filled = [t for t in texts.values() if t]
        if len(filled) == 1 and PAGE_RE.match(normalize_search(filled[0])):
            skipped.append(SkippedLine(raw.ref, f"Sayfa numarası / altbilgi: {filled[0]}"))
            continue
        code, desc, unit = texts.get("code"), texts.get("description"), texts.get("unit")
        label = desc or code
        if kind is ImportKind.KESIF and label and not texts.get("quantity") \
                and TOTAL_RE.match(normalize_search(label)):
            raw_amount = next((texts[k] for k in ("amount", "unit_price") if texts.get(k)), None)
            try:
                amount = _number(next((values[k] for k in ("amount", "unit_price") if texts.get(k)), None),
                                 options.number_format)
            except ValueError:
                amount = None
            totals.append(DeclaredTotal(raw.ref, label, amount, raw_amount))
            skipped.append(SkippedLine(raw.ref, f"Toplam satırı: {label} {raw_amount or ''}".strip()))
            continue
        has_numbers = any(texts.get(k) for k in numeric)
        if not has_numbers and not unit:
            if desc and not code:
                lower_cont = options.desc_only is DescOnlyRows.GROUP and bool(rows) and desc[:1].islower()
                if (options.desc_only is DescOnlyRows.GROUP and not lower_cont) or not rows:
                    context = desc
                    skipped.append(SkippedLine(raw.ref, f"Başlık: {desc}"))
                else:
                    prev = rows[-1]
                    prev.raw["description"] = f"{_text(prev.raw.get('description')) or ''} {desc}".strip()
                    skipped.append(SkippedLine(raw.ref, f"{prev.ref} satırının devamı"))
                    if lower_cont:
                        prev.notes.append(f"Küçük harfle başlayan '{desc}' satırı ({raw.ref}) grup başlığı değil, "
                                          "açıklamanın devamı sayıldı; kontrol edin.")
                continue
            if code and desc:
                context = f"{code} {desc}"
                skipped.append(SkippedLine(raw.ref, f"Başlık: {context}"))
                continue
            skipped.append(SkippedLine(raw.ref, "Sayısal değer içermeyen satır atlandı"))
            continue
        rows.append(StagedRow(raw.ref, raw.page, raw.line, dict(values), conf, context, hidden=raw.hidden))
    result = StagingResult(kind, rows, skipped, options, existing_codes or set(), declared_totals=totals)
    samples = [v for r in rows for k, v in r.raw.items() if k in numeric and isinstance(v, str)]
    guess = detect_format(samples)
    result.format_ambiguous = guess.fmt is None and guess.ambiguous
    validate_all(result)
    for r in rows:
        if r.hidden:
            r.include = False
    return result


def validate_all(result: StagingResult) -> None:
    seen: dict[str, str] = {}
    for row in result.rows:
        _validate_row(row, result)
        if result.kind is ImportKind.FIYAT_KITABI and row.parsed.get("code"):
            key = normalize_code(row.parsed["code"])
            if key in result.existing_codes:
                _fail(row, "Bu poz kodu hedef kitapta zaten var.")
            elif key in seen:
                _fail(row, f"Poz kodu dosyada tekrar ediyor (ilk: {seen[key]}).")
            else:
                seen[key] = row.ref
        if row.status == RowStatus.HATA:
            row.include = False


def _fail(row: StagedRow, msg: str) -> None:
    row.messages.append(msg)
    row.status = RowStatus.HATA


def _warn(row: StagedRow, msg: str) -> None:
    row.messages.append(msg)
    if row.status == RowStatus.GECERLI:
        row.status = RowStatus.UYARI


def _validate_row(row: StagedRow, result: StagingResult) -> None:
    row.parsed, row.messages, row.status, row.low_conf_fields = {}, [], RowStatus.GECERLI, []
    opts = result.options
    labels = {f.key: f.label for f in FIELDS[result.kind]}
    for f in FIELDS[result.kind]:
        v = row.raw.get(f.key)
        try:
            row.parsed[f.key] = _number(v, opts.number_format) if f.numeric else _text(v)
        except ValueError as exc:
            row.parsed[f.key] = None
            if f.key == "montage_price":   # isteğe bağlı alan: satır reddedilmez, değer boş bırakılır
                _warn(row, f"{f.label} okunamadı, boş bırakıldı: {exc}")
            else:
                _fail(row, f"{f.label}: {exc}")
    if result.kind is ImportKind.FIYAT_KITABI:
        _validate_book_row(row, labels)
    else:
        _validate_estimate_row(row)
    if isinstance(row.raw.get("code"), Decimal):
        _warn(row, "Poz kodu dosyada sayı olarak saklanmış; sondaki sıfırlar kaybolmuş olabilir "
                   "(ör. 15.150 → 15.15). Kodu kontrol edin.")
    for note in row.notes:
        _warn(row, note)
    if row.hidden:
        _warn(row, "Excel'de gizli satır; varsayılan olarak aktarılmaz (dahil etmek için satırı seçin).")
    if result.format_ambiguous and not row.edited and row.status != RowStatus.HATA:
        numeric = {f.key for f in FIELDS[result.kind] if f.numeric}
        amb = [(k, v) for k, v in row.raw.items() if k in numeric and isinstance(v, str) and classify(v) == "AMBIGUOUS"]
        if amb:
            k, v = amb[0]
            row.messages.append(f"Sayı biçimi belirsiz: '{v}' {opts.number_format.label} biçimiyle {row.parsed.get(k)} "
                                "okundu. Dosyanın sayı biçimini eşleme adımında doğrulayıp satırları onaylayın.")
            row.status = RowStatus.ONAY_GEREKLI
    if not row.edited:
        # OCR'nin tanıyamadığı glif "?" olarak, çözülemeyen kodlama "�" olarak gelir: güven değeri yüksek olsa da onay
        garbled = [k for k, v in row.raw.items() if isinstance(v, str) and (
            "�" in v or ("?" in v and row.confidences.get(k) is not None))]
        if garbled and row.status != RowStatus.HATA:
            names = ", ".join(labels.get(k, k) for k in garbled)
            row.messages.append(f"Okunamayan karakter: {names} ({row.raw[garbled[0]]}); kaynağa bakıp düzeltin ya "
                                "da onaylayın.")
            row.status = RowStatus.ONAY_GEREKLI
            row.low_conf_fields = list(garbled)
        low = [k for k, c in row.confidences.items() if c is not None and c < opts.ocr_threshold]
        if low:
            row.low_conf_fields = list(dict.fromkeys(row.low_conf_fields + low))
            names = ", ".join(labels.get(k, k) for k in low)
            row.messages.append(f"OCR güveni düşük: {names} (en düşük %{row.min_confidence:.0f})")
            if row.status != RowStatus.HATA:
                row.status = RowStatus.ONAY_GEREKLI


def _validate_book_row(row: StagedRow, labels: dict[str, str]) -> None:
    p = row.parsed
    for key in ("code", "description", "unit", "unit_price"):
        if p.get(key) in (None, "") and not any(m.startswith(labels[key]) for m in row.messages):
            _fail(row, f"{labels[key]} eksik.")
    price = p.get("unit_price")
    if isinstance(price, Decimal):
        if price < 0:
            _fail(row, "Birim fiyat negatif olamaz.")
        elif decimal_places(price) > PRICE_DB_PLACES:
            _fail(row, f"Birim fiyat en fazla {PRICE_DB_PLACES} ondalık basamak içerebilir.")
        elif price == 0:
            _warn(row, "Birim fiyat sıfır.")
    montage = p.get("montage_price")
    if isinstance(montage, Decimal):
        if montage < 0:
            _warn(row, "Montaj bedeli negatif olamaz; boş bırakıldı.")
            p["montage_price"] = None
        elif decimal_places(montage) > PRICE_DB_PLACES:
            _warn(row, f"Montaj bedeli en fazla {PRICE_DB_PLACES} ondalık basamak içerebilir; boş bırakıldı.")
            p["montage_price"] = None
    if not p.get("category") and row.context:
        p["category"] = row.context


def _validate_estimate_row(row: StagedRow) -> None:
    p = row.parsed
    if not p.get("code") and not p.get("description"):
        _fail(row, "Poz no ya da imalat açıklamasından en az biri olmalıdır.")
    qty = p.get("quantity")
    if qty is None and not any(m.startswith("Miktar") for m in row.messages):
        _fail(row, "Miktar eksik.")
    elif isinstance(qty, Decimal):
        if qty < 0:
            _fail(row, "Miktar negatif olamaz.")
        elif decimal_places(qty) > QUANTITY_PLACES:
            _fail(row, f"Miktar en fazla {QUANTITY_PLACES} ondalık basamak içerebilir.")
        elif qty == 0:
            _warn(row, "Miktar sıfır.")
    for key, label in (("unit_price", "Birim fiyat"), ("amount", "Tutar")):
        v = p.get(key)
        if isinstance(v, Decimal) and v < 0:
            _fail(row, f"{label} negatif olamaz.")
        elif isinstance(v, Decimal) and decimal_places(v) > PRICE_DB_PLACES:
            _warn(row, f"{label} {v} kayıtta {PRICE_DB_PLACES} ondalık basamağa yuvarlanacak "
                       f"({quantize(v, PRICE_DB_PLACES)}).")
    if not p.get("unit"):
        _warn(row, "Birim belirtilmemiş.")
    price, amount = p.get("unit_price"), p.get("amount")
    if isinstance(qty, Decimal) and isinstance(price, Decimal) and isinstance(amount, Decimal) \
            and row.status != RowStatus.HATA:
        expected = line_amount(qty, price)
        if abs(expected - amount) > Decimal("0.01"):
            _warn(row, f"Tutar miktar × birim fiyatla uyuşmuyor (hesaplanan {expected}, dosyada {amount}).")
            ocr_numeric = [key for key in ("quantity", "unit_price", "amount")
                           if row.confidences.get(key) is not None]
            if ocr_numeric:
                row.messages.append("OCR ile okunan miktar/fiyat/tutar birbiriyle uyuşmuyor; kaynağa bakıp düzeltin "
                                    "ya da açıkça onaylayın.")
                row.status = RowStatus.ONAY_GEREKLI
                row.low_conf_fields = list(dict.fromkeys(row.low_conf_fields + ocr_numeric))
    if not p.get("work_group") and row.context:
        p["work_group"] = row.context


def fill_missing(result: StagingResult, key: str, value: str, rows: list[StagedRow] | None = None) -> int:
    """Seçili (ya da bütün) satırlarda boş olan alanı kullanıcının girdiği değerle doldurur.

    Kaynakta bulunmayan bilgiyi program uydurmaz; bu yalnızca kullanıcının açık kararıyla yapılır ve
    satırlar "düzeltildi" olarak işaretlenip içe aktarma raporunda görünür.
    """
    value = value.strip()
    if not value:
        raise ValueError("Doldurulacak değer boş olamaz.")
    n = 0
    for row in rows if rows is not None else result.rows:
        if row.parsed.get(key) in (None, ""):
            row.raw[key] = value
            row.edited = True
            row.include = True
            n += 1
    validate_all(result)
    return n


def edit_cell(result: StagingResult, row: StagedRow, key: str, value: str) -> None:
    """Kullanıcı düzeltmesi: değer yazılır, satır 'düzeltildi' sayılır, hepsi yeniden doğrulanır."""
    row.raw[key] = value.strip() or None
    row.edited = True
    row.confirmed = False
    row.include = True
    validate_all(result)
