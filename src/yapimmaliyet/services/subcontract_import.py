"""Alt yüklenici teklifini Excel dosyasından okuma ve iş paketi kalemleriyle eşleştirme.

Dosya türleri: Excel (.xlsx), CSV ve metin tabanlı PDF (taranmış PDF reddedilir). Biçim (esnek): bir başlık satırı ve altında kalemler. Tanınan başlıklar: Poz / Poz no, Tanım / Açıklama / İş kalemi,
Birim, Miktar, Birim fiyat (zorunlu), Kapsam dışı / Hariç (dolu ya da "evet/x/1" ise kapsam dışı), Not.
Eşleştirme: poz kodu (varsa) sonra normalize tanım. Hiçbir satır sessizce atılmaz: eşleşmeyen, yinelenen ve hatalı
satırlar nedenleriyle raporlanır; pakette olup dosyada bulunmayan kalemler "fiyatsız" olarak bildirilir.
`BidImport.clean` doğruysa dosya paketle birebir uyumludur.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from yapimmaliyet.domain.parsing import parse_user_decimal
from yapimmaliyet.domain.text_tr import normalize_search
from yapimmaliyet.fileio import excel_reader
from yapimmaliyet.infra.errors import ValidationError
from yapimmaliyet.services.subcontract_service import PackageItem

HEADERS = {
    "code": ("poz", "poz no", "poz kodu", "kod", "sira no poz"),
    "desc": ("tanim", "aciklama", "is kalemi", "imalatin tanimi", "kalem", "isin tanimi"),
    "unit": ("birim",),
    "qty": ("miktar", "metraj"),
    "price": ("birim fiyat", "teklif birim fiyati", "birim fiyati", "fiyat"),
    "scope": ("kapsam disi", "haric", "kapsam disi mi"),
    "note": ("not", "teklif notu", "firma notu"),
}
_TRUE = {"evet", "e", "x", "1", "true", "dogru", "var"}


@dataclass
class ImportRow:
    ref: str
    code: str
    desc: str
    unit: str
    qty: Decimal | None
    price: Decimal | None
    out_of_scope: bool
    note: str


@dataclass
class Rejected:
    ref: str
    text: str
    reason: str


@dataclass
class BidImport:
    sheet: str
    source_rows: int = 0
    lines: dict[int, tuple[Decimal, bool, str]] = field(default_factory=dict)   # kalem → (fiyat, kapsamda, not)
    matched: list[tuple[ImportRow, PackageItem]] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    missing: list[PackageItem] = field(default_factory=list)                      # dosyada olmayan paket kalemleri
    warnings: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.rejected and not self.missing

    def summary(self) -> str:
        return (f"Sayfa '{self.sheet}': {self.source_rows} satır okundu, {len(self.matched)} eşleşti, "
                f"{len(self.rejected)} reddedildi; pakette dosyada olmayan kalem: {len(self.missing)}.")


def _key(text) -> str:
    return normalize_search(str(text or "")).strip()


def _find_header(table) -> tuple[int, dict[str, int]] | None:
    for i, row in enumerate(table.rows[:40]):
        cols: dict[str, int] = {}
        for j, v in enumerate(row.values):
            k = _key(v)
            if not k:
                continue
            for name, words in HEADERS.items():
                if name not in cols and k in words:
                    cols[name] = j
        if "price" in cols and ("desc" in cols or "code" in cols):
            return i, cols
    return None


def _num(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, Decimal):
        return value
    return parse_user_decimal(str(value))


def read_bid_file(path: Path | str, items: list[PackageItem]) -> BidImport:
    """Teklif dosyasını (Excel, CSV ya da metin tabanlı PDF) okuyup paket kalemleriyle eşleştirir."""
    from yapimmaliyet.fileio import csv_reader, pdf_text
    from yapimmaliyet.fileio.detect import FileKind, detect_kind

    path = Path(path)
    kind = detect_kind(path)                    # bozuk / boş / desteklenmeyen dosya burada açık hatayla reddedilir
    try:
        if kind is FileKind.XLSX:
            tables = excel_reader.read_xlsx(path)
        elif kind is FileKind.CSV:
            tables = [csv_reader.read_csv(path)]
        else:
            pdf = pdf_text.analyze_pdf(path)
            if not pdf.text_pages:
                raise ValidationError("Bu PDF taranmış görüntü içeriyor; metin okunamadı. Metin tabanlı PDF ya da Excel "
                                      "kullanın (taranmış teklif için önce OCR ile metne çevirin).")
            tables = [pdf_text.read_pdf_text(path, pdf.text_pages)]
    except ValidationError:
        raise
    except Exception as exc:   # bozuk dosya kullanıcıya anlaşılır hata olarak döner
        raise ValidationError("Teklif dosyası okunamadı (bozuk ya da desteklenmeyen biçim).", detail=str(exc)) from exc
    return read_bid_tables(tables, items)


def read_bid_sheet(path: Path | str, items: list[PackageItem]) -> BidImport:   # geriye uyumluluk
    return read_bid_file(path, items)


def read_bid_tables(tables, items: list[PackageItem]) -> BidImport:
    found = [(t, _find_header(t)) for t in tables]
    found = [(t, h) for t, h in found if h is not None]
    if not found:
        raise ValidationError("Dosyada teklif tablosu bulunamadı: 'Birim fiyat' sütunu ile 'Tanım' ya da 'Poz' "
                              "sütununu içeren bir başlık satırı gerekir. Şablonu kaydedip doldurabilirsiniz.")
    table, (hrow, cols) = found[0]
    res = BidImport(table.name)
    if len(found) > 1:
        res.warnings.append(f"{len(found)} sayfada teklif tablosu var; yalnız '{table.name}' okundu.")
    by_code = {_key(i.code): i for i in items if _key(i.code)}
    by_desc = {_key(i.description): i for i in items}
    taken: dict[int, str] = {}
    for row in table.rows[hrow + 1:]:
        get = lambda name: row.cell(cols[name]) if name in cols else None   # noqa: E731
        code, desc = str(get("code") or "").strip(), str(get("desc") or "").strip()
        raw_price = get("price")
        if not code and not desc and (raw_price is None or str(raw_price).strip() == ""):
            continue                                           # tamamen boş satır
        res.source_rows += 1
        text = f"{code} {desc}".strip()
        try:
            price = _num(raw_price)
            qty = _num(get("qty"))
        except ValueError:
            res.rejected.append(Rejected(row.ref, text, "Sayı okunamadı (birim fiyat ya da miktar)"))
            continue
        item = by_code.get(_key(code)) if _key(code) else None
        if item is None and _key(desc):
            item = by_desc.get(_key(desc))
        if item is None:
            res.rejected.append(Rejected(row.ref, text, "Pakette karşılığı olan kalem yok"))
            continue
        if item.id in taken:
            res.rejected.append(Rejected(row.ref, text, f"Aynı kalem dosyada yinelenmiş (ilk: {taken[item.id]})"))
            continue
        scope_txt = _key(get("scope"))
        out = scope_txt in _TRUE
        if price is None:
            if not out:
                res.rejected.append(Rejected(row.ref, text, "Birim fiyat boş"))
                continue
            price = Decimal(0)
        if price < 0:
            res.rejected.append(Rejected(row.ref, text, "Birim fiyat negatif"))
            continue
        unit = str(get("unit") or "").strip()
        if unit and _key(unit) != _key(item.unit):
            res.warnings.append(f"{row.ref}: birim farklı (dosya '{unit}', paket '{item.unit}'); fiyat paket birimine uygulandı.")
        if qty is not None and qty != item.quantity:
            res.warnings.append(f"{row.ref}: miktar farklı (dosya {qty}, paket {item.quantity}); paket miktarı kullanılır.")
        taken[item.id] = row.ref
        r = ImportRow(row.ref, code, desc, unit, qty, price, out, str(get("note") or "").strip())
        res.matched.append((r, item))
        res.lines[item.id] = (price, not out, r.note)
    res.missing = [i for i in items if i.id not in taken]
    return res


def write_template(path: Path | str, items: list[PackageItem], title: str = "") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Teklif"
    if title:
        ws["A1"] = f"Teklif formu — {title}"
        ws["A1"].font = Font(bold=True, size=13)
    heads = ["Poz no", "Tanım", "Birim", "Miktar", "Birim fiyat", "Kapsam dışı", "Not"]
    ws.append([])
    ws.append(heads)
    for c in ws[3]:
        c.font = Font(bold=True)
    for it in items:
        ws.append([it.code, it.description, it.unit, float(it.quantity), None, None, None])
    for col, w in zip("ABCDEFG", (14, 50, 10, 14, 16, 14, 30)):
        ws.column_dimensions[col].width = w
    path = Path(path)
    wb.save(path)
    return path
