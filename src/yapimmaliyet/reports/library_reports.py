"""Kütüphane raporları: fiyat listesi sürüm farkı ve analiz karşılaştırması."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.domain.analysis_compare import AnalysisComparison
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
from yapimmaliyet.services.book_update_service import Change, UpdatePreview


def _meta(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs) + [("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")), ("Uygulama sürümü", __version__)]


def book_diff(pv: UpdatePreview, old_label: str, new_label: str) -> ReportDoc:
    c = pv.counts()
    summary = Table([Col("Durum", width=3), Col("Poz", ColKind.INT, 1)],
                    [[k.label, c[k]] for k in Change] + [["Eski listede olup yenide olmayan", len(pv.missing_in_file)],
                                                         ["Tanımı farklı", sum(r.desc_changed for r in pv.rows)]])
    diff_cols = [Col("Poz No", width=1.2), Col("Tanım", width=4), Col("Birim", width=0.8),
                 Col("Eski fiyat", ColKind.PRICE, 1), Col("Yeni fiyat", ColKind.PRICE, 1), Col("Fark", ColKind.PRICE, 1),
                 Col("Değişim", ColKind.PERCENT, 0.8), Col("Not", width=1.4)]

    def rows(change: Change):
        out = []
        for r in pv.by_change(change):
            unit = f"{r.old_unit} → {r.unit}" if change is Change.BIRIM else r.unit
            notes = "; ".join(n for n, on in (("tanım farklı", r.desc_changed), ("montaj bedeli değişti",
                                                                                  r.montage_changed)) if on)
            diff = None if r.old_price is None else r.new_price - r.old_price
            out.append([r.code, r.description, unit, r.old_price, r.new_price, diff, r.change_ratio, notes])
        return out

    changed = rows(Change.FIYAT)
    total_old = sum((r[3] for r in changed), Decimal(0))
    total_new = sum((r[4] for r in changed), Decimal(0))
    sections = [Section("Özet", summary),
                Section(f"Fiyatı değişen pozlar ({len(changed)})",
                        Table(diff_cols, changed, ["Toplam (birim fiyatlar)", "", "", total_old, total_new,
                                                   total_new - total_old, None, ""])),
                Section(f"Birimi değişen pozlar ({c[Change.BIRIM]})", Table(diff_cols, rows(Change.BIRIM))),
                Section(f"Yeni pozlar ({c[Change.YENI]})", Table(diff_cols, rows(Change.YENI))),
                Section(f"Eski listede olup yenide olmayan pozlar ({len(pv.missing_in_file)})",
                        Table([Col("Poz No", width=1.2), Col("Tanım", width=5), Col("Birim", width=0.8),
                               Col("Eski fiyat", ColKind.PRICE, 1)],
                              [[m.code, m.description, m.unit, m.price] for m in pv.missing_in_file]))]
    notes = ["Karşılaştırma poz koduna göre yapılır (normalize kod). Fiyat farkı = yeni − eski; değişim = yeni / eski − 1.",
             "Birimi değişen pozlarda fiyatlar doğrudan karşılaştırılamaz. Eski kayıtlar silinmez; her liste ayrı sürümdür.",
             "'Aynı kalan' pozlar raporda listelenmez; sayısı özet tablosundadır."]
    return ReportDoc("Fiyat Listesi Sürüm Farkı", _meta(("Eski liste", old_label), ("Yeni liste", new_label)),
                     sections, notes)


def analysis_comparison(cmp: AnalysisComparison, label_a: str, label_b: str) -> ReportDoc:
    cols = [Col("Durum", width=1), Col("Kod", width=1), Col("Girdi", width=3), Col("Bölüm", width=0.9),
            Col("Birim", width=0.6), Col("Miktar 1", ColKind.QTY, 0.9), Col("B.fiyat 1", ColKind.PRICE, 0.9),
            Col("Tutar 1", ColKind.MONEY, 0.9), Col("Miktar 2", ColKind.QTY, 0.9), Col("B.fiyat 2", ColKind.PRICE, 0.9),
            Col("Tutar 2", ColKind.MONEY, 0.9), Col("Fark", ColKind.MONEY, 0.9), Col("Miktar etkisi", ColKind.MONEY, 0.9),
            Col("Fiyat etkisi", ColKind.MONEY, 0.9), Col("Etkileşim", ColKind.MONEY, 0.8)]
    rows = []
    for r in cmp.rows:
        a, b = r.a, r.b
        unit = (a or b).unit if r.status.value != "BIRIM" else f"{a.unit} / {b.unit}"
        rows.append([r.status.label, r.code or "", r.description, r.section or "", unit or "",
                     a.quantity if a else None, a.unit_price if a else None, a.amount if a else None,
                     b.quantity if b else None, b.unit_price if b else None, b.amount if b else None,
                     r.amount_diff, r.qty_effect, r.price_effect, r.interaction])
    qe, pe, ie = cmp.effect_totals()
    total_row = ["Toplam", "", "", "", "", None, None, sum((r[7] or 0 for r in rows), Decimal(0)), None, None,
                 sum((r[10] or 0 for r in rows), Decimal(0)), sum((r[11] for r in rows), Decimal(0)), qe, pe, ie]
    sec = Table([Col("Bölüm", width=2), Col("1. analiz", ColKind.MONEY, 1), Col("2. analiz", ColKind.MONEY, 1),
                 Col("Fark", ColKind.MONEY, 1)], [[k, x, y, y - x] for k, (x, y) in cmp.sections.items()])
    totals = Table([Col("Gösterge", width=3), Col("Tutar (TL)", ColKind.MONEY, 1)],
                   [["1. analiz birim fiyatı", cmp.total_a], ["2. analiz birim fiyatı", cmp.total_b],
                    ["Fark", cmp.total_diff], ["Miktar etkisi (girdiler)", qe], ["Fiyat etkisi (girdiler)", pe],
                    ["Etkileşim", ie]])
    return ReportDoc("Birim Fiyat Analizi Karşılaştırması",
                     _meta(("1. analiz", label_a), ("2. analiz", label_b)),
                     [Section("Toplamlar", totals), Section("Bölüm toplamları", sec),
                      Section(f"Girdiler ({len(rows)})", Table(cols, rows, total_row))], cmp.notes)
