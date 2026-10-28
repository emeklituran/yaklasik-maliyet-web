"""Mukayeseli keşif ve icmal raporu."""

from __future__ import annotations

from datetime import datetime

from yapimmaliyet import __version__
from yapimmaliyet.domain.estimate_compare import Comparison, RowStatus
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table

SIGNATURES = ["Hazırlayan: ……………………………   Tarih: ……………   İmza: ……………",
              "Kontrol eden: ……………………………   Tarih: ……………   İmza: ……………",
              "Onaylayan: ……………………………   Tarih: ……………   İmza: ……………"]


def estimate_comparison(cmp: Comparison, name: str, first_label: str, second_label: str,
                        revision: str = "1") -> ReportDoc:
    sm = cmp.summary
    icmal = Table([Col("Gösterge", width=3), Col("Tutar (TL)", ColKind.MONEY, 1.2), Col("Kalem", ColKind.INT, 0.7)], [
        ["İlk keşif toplamı (fiyatlı kalemler)", sm.first_total, None],
        ["Son keşif toplamı (fiyatlı kalemler)", sm.second_total, None],
        ["Toplam fark", sm.difference, None],
        ["Değişim oranı", None, None] if sm.ratio is None else ["Değişim oranı (%)", sm.ratio * 100, None],
        ["Miktarı artan kalemlerin farkı", sm.increased, sm.counts.get(RowStatus.ARTAN, 0)],
        ["Miktarı azalan kalemlerin farkı", sm.decreased, sm.counts.get(RowStatus.AZALAN, 0)],
        ["Yeni eklenen kalemler", sm.added, sm.counts.get(RowStatus.YENI, 0)],
        ["Çıkarılan kalemler (−)", sm.removed, sm.counts.get(RowStatus.CIKAN, 0)],
        ["Yalnız fiyatı değişen kalemler", sm.price_only, sm.counts.get(RowStatus.FIYAT, 0)],
        ["Miktar etkisi (eşleşen kalemler)", sm.qty_effect, None],
        ["Fiyat etkisi (eşleşen kalemler)", sm.price_effect, None],
        ["Etkileşim (miktar × fiyat farkı)", sm.interaction, None],
        ["Çözümlenmemiş kalem (fiyatı yok; toplamlara katılmadı)", None, sm.unresolved]])
    cols = [Col("Durum", width=1.2), Col("İlk poz", width=1.1), Col("Son poz", width=1.1), Col("Tarif", width=3),
            Col("Birim", width=0.6), Col("İlk miktar", ColKind.QTY, 0.8), Col("Son miktar", ColKind.QTY, 0.8),
            Col("Miktar farkı", ColKind.QTY, 0.8), Col("İlk B.F.", ColKind.PRICE, 0.9), Col("Son B.F.", ColKind.PRICE, 0.9),
            Col("İlk tutar", ColKind.MONEY, 1), Col("Son tutar", ColKind.MONEY, 1), Col("Tutar farkı", ColKind.MONEY, 1),
            Col("Miktar etkisi", ColKind.MONEY, 0.9), Col("Fiyat etkisi", ColKind.MONEY, 0.9), Col("Gerekçe", width=1.6)]
    rows = []
    for r in cmp.rows:
        f, s = r.first, r.second
        eff = r.effects()
        rows.append([r.status.label + (" (çözümlenmemiş)" if r.unresolved else ""), f.code if f else "",
                     s.code if s else "", (f or s).description or "", (f or s).unit or "",
                     f.quantity if f else None, s.quantity if s else None, r.qty_diff,
                     f.unit_price if f else None, s.unit_price if s else None, f.amount if f else None,
                     s.amount if s else None, r.amount_diff, eff[0] if eff else None, eff[1] if eff else None,
                     r.basis])
    total = ["Toplam", "", "", "", "", None, None, None, None, None, sm.first_total, sm.second_total, sm.difference,
             sm.qty_effect, sm.price_effect, ""]
    sug = Table([Col("İlk poz", width=1.2), Col("İlk tarif", width=3), Col("Son poz", width=1.2),
                 Col("Son tarif", width=3), Col("Neden", width=2)],
                [[x.first.code or "", x.first.description or "", x.second.code or "", x.second.description or "",
                  x.reason] for x in cmp.suggestions])
    meta = [("Karşılaştırma", name), ("İlk keşif", first_label), ("Son keşif", second_label),
            ("Revizyon", revision), ("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")),
            ("Durum", "Güncel veriden üretildi; kesin kayıt değildir"), ("Uygulama sürümü", __version__)]
    return ReportDoc("Mukayeseli Keşif ve İcmal", meta, [
        Section("İcmal", icmal),
        Section(f"Kalem bazında karşılaştırma ({len(rows)})", Table(cols, rows, total)),
        Section(f"Onay bekleyen eşleştirme önerileri ({len(cmp.suggestions)})", sug),
        Section("Onay", paragraphs=SIGNATURES)], cmp.notes)
