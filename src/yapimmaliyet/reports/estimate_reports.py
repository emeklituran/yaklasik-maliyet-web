"""Keşif kontrol raporları: eşleşmeyen/sorunlu kalemler ve poz fiyatlarının kitaplar arası karşılaştırması.

Bu raporlar kesin kayıttan değil, keşfin ve kütüphanenin güncel durumundan üretilir; kontrol amaçlıdır.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal

from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table

STATUS = {"ESLESMEDI": "Eşleşmedi", "ONERILDI": "Öneri onaylanmadı", "ONAYLANDI": "Onaylı",
          "ERTELENDI": "Ertelendi"}


def _meta(project, estimate, app_version: str) -> list[tuple[str, str]]:
    return [("Proje", project.name), ("Keşif", estimate.display_name),
            ("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")),
            ("Durum", "Kontrol raporu — güncel veriden üretildi; kesin kayıt değildir"),
            ("Uygulama sürümü", app_version)]


def match_issues(project, estimate, items, app_version: str) -> ReportDoc:
    """Eşleşmemiş, onaylanmamış, ertelenmiş, birim farkı kabul edilmemiş ve keşifte mükerrer kodlu kalemler."""
    codes = Counter(normalize_code(i.code) for i in items if i.code)
    rows = []
    for it in items:
        problems = []
        if it.match_status != "ONAYLANDI":
            problems.append(STATUS[it.match_status])
        if it.unit_mismatch and not it.unit_mismatch_ack:
            problems.append(f"Birim farkı: keşif '{it.unit}', poz '{it.matched_unit}'")
        if it.code and codes[normalize_code(it.code)] > 1:
            problems.append(f"Keşifte {codes[normalize_code(it.code)]} kez geçiyor")
        if problems:
            rows.append([it.line_no, it.code or "", it.description or "", it.unit or "", it.quantity,
                         it.matched_code or "", "; ".join(problems)])
    counts = Counter(i.match_status for i in items)
    summary = Table([Col("Gösterge", width=3), Col("Kalem", ColKind.INT, 1)],
                    [[STATUS[k], counts.get(k, 0)] for k in STATUS]
                    + [["Birim farkı kabul edilmemiş", sum(i.unit_mismatch and not i.unit_mismatch_ack for i in items)],
                       ["Mükerrer kodlu kalem", sum(1 for i in items if i.code and codes[normalize_code(i.code)] > 1)],
                       ["Toplam kalem", len(items)]])
    detail = Table([Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("İmalat", width=4),
                    Col("Birim", width=0.6), Col("Miktar", ColKind.QTY, 0.9), Col("Eşleşen poz", width=1.2),
                    Col("Sorun", width=3)], rows)
    notes = ["Kesinleştirme için bütün kalemlerin onaylı ve birim farklarının kabul edilmiş olması gerekir.",
             "Mükerrer kod her zaman hata değildir (farklı iş gruplarında aynı poz olabilir); kontrol edin."]
    return ReportDoc("Eşleşmeyen ve Sorunlu Kalemler", _meta(project, estimate, app_version),
                     [Section("Özet", summary), Section(f"Sorunlu kalemler ({len(rows)})", detail)], notes,
                     landscape=True)


def price_comparison(project, estimate, items, prices: dict[str, list[tuple[int, Decimal]]],
                     books: list[tuple[int, str]], app_version: str) -> ReportDoc:
    """Keşifteki her pozun kütüphanedeki fiyat kitaplarındaki birim fiyatı yan yana.

    `prices`: normalize kod → [(kitap kimliği, birim fiyat)], `books`: [(kitap kimliği, sütun başlığı)].
    """
    cols = [Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("İmalat", width=3), Col("Birim", width=0.6)]
    cols += [Col(title, ColKind.PRICE, 1.1) for _, title in books] + [Col("En yüksek / en düşük", ColKind.PERCENT, 1)]
    rows, missing = [], 0
    for it in items:
        if not it.code:
            continue
        found = dict(prices.get(normalize_code(it.code), []))
        if not found:
            missing += 1
        values = [found.get(bid) for bid, _ in books]
        present = [v for v in values if v]
        spread = (max(present) / min(present) - 1) if len(present) > 1 else None
        rows.append([it.line_no, it.code, it.description or "", it.unit or ""] + values + [spread])
    notes = [f"{missing} pozun kodu hiçbir fiyat kitabında bulunamadı." if missing else
             "Keşifteki bütün kodlar en az bir kitapta bulundu.",
             "Taslak durumundaki kitaplar karşılaştırmaya alınmaz. Fark sütunu: en yüksek fiyat / en düşük fiyat − 1."]
    return ReportDoc("Poz Birim Fiyat Karşılaştırması", _meta(project, estimate, app_version),
                     [Section("Kitaplara göre birim fiyatlar", Table(cols, rows))], notes)
