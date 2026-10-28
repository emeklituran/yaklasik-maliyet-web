"""Taşeron hakedişi dosyası. Sayfa sırası ve sütunlar kullanıcının örneklerindeki düzenle aynıdır
(Silopi/Gasway, Bodrum/Refleks, Eskişehir/Indotek): 1-Ön Kapak, 2-Ödeme Cetveli, 3-İcmal, 4-Hakediş Takip Tablosu,
5-Hakediş İmalat İcmali, 6-Yeşil Defter; ek olarak kesinti dökümü ve kontrol notları.
"""

from __future__ import annotations

from yapimmaliyet.domain.progress import ZERO
from yapimmaliyet.reports.hakedis_common import (
    DISCLAIMER,
    amount,
    contract_rows,
    control_page,
    d,
    deduction_label,
    deductions_page,
    green_book,
    header,
    money,
    signatures,
)
from yapimmaliyet.reports.model import Col, ColKind, Table
from yapimmaliyet.reports.packet import KeyValues, Packet, Page, TableBlock, Text


def _cover(view) -> Page:
    c, p = view.contract, view.payment
    kv = [("İşveren", c.employer), ("İşin adı", c.title), ("Taşeron (uzman ekip)", c.contractor),
          ("Hakediş no", str(p.no) + (f" (düzeltme rev. {p.revision})" if p.revision else "")),
          ("Hakediş tarihi", d(p.payment_date or p.period_end))] + contract_rows(view)
    return Page("1-Ön Kapak", "HAKEDİŞ RAPORU", [KeyValues(kv), signatures(view)], header(view, 1))


def _payment_sheet(view) -> Page:
    """Ödeme cetveli. KDV tabanı BRUT ise KDV kesintilerden önce, NET ise kesintiler sonrası tutar üzerinden."""
    r, p, cur = view.result, view.payment, view.contract.currency
    rows = [["A", f"{d(p.period_end)} tarihine kadar yapılan işlerin toplam tutarı", r.total_cum],
            ["", "   – sözleşme fiyatlarıyla yapılan işler", r.work_cum],
            ["", "   – fiyat farkı (kümülatif)", r.price_diff_cum],
            ["B", "Bir önceki hakedişin toplam tutarı", r.total_prev],
            ["C", f"Bu hakedişin tutarı (A − B)  [{d(p.period_start)} – {d(p.period_end)}]", r.this_total]]
    bold = [0, 4]
    if view.contract.vat_base == "BRUT":
        rows.append(["D", f"KDV (C × %{view.contract.vat_rate * 100:.0f})", r.vat])
        rows.append(["E", "Tahakkuk tutarı (C + D)", r.this_total + r.vat])
        bold.append(len(rows) - 1)
    rows.append(["", "KESİNTİLER VE MAHSUPLAR", None])
    bold.append(len(rows) - 1)
    for dr in r.deductions:
        rows.append(["", "   " + deduction_label(dr.type), dr.amount])
    rows.append(["F", "Kesintiler ve mahsuplar toplamı", r.deductions_total])
    bold.append(len(rows) - 1)
    if view.contract.vat_base == "NET":
        rows.append(["G", "Bu hakedişin net tutarı (C − F, KDV'ye dayalı kesintiler hariç)", r.net_before_vat])
        rows.append(["H", f"KDV (G × %{view.contract.vat_rate * 100:.0f}, kesintiler sonrası)", r.vat])
    table = Table([Col("", width=0.4), Col("Açıklama", width=6), Col(f"Tutar ({cur})", ColKind.MONEY, 1.8)], rows,
                  ["", "TAŞERONA ÖDENECEK TUTAR", r.payable])
    blocks = [TableBlock(table, bold_rows=tuple(bold))]
    if abs(r.price_change_effect) > 0:
        blocks.append(Text(f"Bu dönem tutarına onaylı fiyat değişikliğinin etkisi dahildir: "
                           f"{money(r.price_change_effect, cur)} (kontrol notlarına bakın).", "warning"))
    return Page("2-Ödeme Cetveli", "ÖDEME CETVELİ", blocks + [signatures(view)], header(view, 2))


def _summary(view) -> Page:
    """İcmal: sözleşme fiyatlarıyla yapılan işler ve hakediş bazında fiyat farkları."""
    r, cur = view.result, view.contract.currency
    rows = [[1, "Yapılan işler icmalinden (sözleşme fiyatlarıyla)", r.work_cum, None]]
    for i, h in enumerate(view.timeline, start=2):
        ff = amount(h.get("price_diff"))
        if ff:
            rows.append([i, f"{h['no']} nolu hakediş fiyat farkı ({d(h.get('period_end'))})", None, ff])
    table = Table([Col("Sıra No", ColKind.INT, 0.6), Col("İşin cinsi", width=5),
                   Col(f"Sözleşme fiyatlarıyla yapılan işler ({cur})", ColKind.MONEY, 2),
                   Col(f"Fiyat farkı ({cur})", ColKind.MONEY, 1.6)], rows,
                  [None, "TOPLAM", r.work_cum, r.price_diff_cum])
    return Page("3-İcmal", f"{d(view.payment.period_end)} TARİHİNE KADAR YAPILAN İŞLER İCMALİ",
                [TableBlock(table), Text(f"Genel toplam (A + B): {money(r.total_cum, cur)}", "heading"),
                 signatures(view)], header(view, 3))


def _tracking(view) -> Page:
    """Hakediş ödemeleri takip tablosu: her hakediş için tutar, KDV, kesinti türleri ve net ödeme."""
    names = [dr.type.name for dr in view.result.deductions]
    timeline = view.timeline
    for h in timeline:
        names += [n for n in h.get("deductions", {}) if n not in names]
    cols = [Col("No", ColKind.INT, 0.5), Col("Tarih", width=1), Col("Hakediş tutarı (A)", ColKind.MONEY, 1.4),
            Col("KDV (B)", ColKind.MONEY, 1.2)] + [Col(n, ColKind.MONEY, 1.1) for n in names] + \
           [Col("Kesinti toplamı (K)", ColKind.MONEY, 1.3), Col("Net ödeme (A + B − K)", ColKind.MONEY, 1.4)]
    rows, totals = [], [ZERO] * (len(names) + 4)
    for h in timeline:
        vals = [amount(h["this_total"]), amount(h["vat"])] + [amount(h["deductions"].get(n)) for n in names] + \
               [amount(h["deductions_total"]), amount(h["payable"])]
        totals = [a + b for a, b in zip(totals, vals)]
        rows.append([h["no"], d(h.get("payment_date") or h.get("period_end"))] + vals)
    table = Table(cols, rows, [None, "TOPLAM"] + totals)
    return Page("4-Hak.Takip Tablosu", "HAKEDİŞ ÖDEMELERİ TAKİP TABLOSU",
                [TableBlock(table), Text("Satırlar önceki hakedişlerin kesinleşmiş kayıtlarından alınmıştır.", "note"),
                 signatures(view)], header(view, 4), landscape=True)


def work_rows(view) -> tuple[list[list], tuple[int, ...]]:
    """Hakediş imalat icmali satırları; kalemler grup adına göre başlıklandırılır."""
    rows, bold, group = [], [], object()
    for ln in view.result.lines:
        it = ln.item
        if it.group_name != group:
            group = it.group_name
            if group:
                bold.append(len(rows))
                rows.append([None, None, group.upper()] + [None] * 8)
        unit = "%" if it.method == "GOTURU" else it.unit
        rows.append([it.line_no, it.code, it.description, unit, it.price, ln.cum_qty, ln.input.prev_qty,
                     ln.input.period_qty, ln.cum_amount, ln.prev_amount, ln.period_amount])
    r = view.result
    if r.price_diff_cum:
        rows.append([None, "FİYAT FARKI", "Fiyat farkı (kümülatif)", None, None, None, None, None, r.price_diff_cum,
                     r.price_diff_prev, r.price_diff_period])
    if abs(r.price_change_effect) > 0:
        rows.append([None, None, "Onaylı fiyat değişikliğinin önceki miktarlara etkisi", None, None, None, None, None,
                     None, -r.price_change_effect, r.price_change_effect])
    return rows, tuple(bold)


def _works(view) -> Page:
    r, cur = view.result, view.contract.currency
    rows, bold = work_rows(view)
    cols = [Col("S.No", ColKind.INT, 0.45), Col("Poz No", width=1.1), Col("İmalatın açıklaması", width=3.6),
            Col("Birim", width=0.55), Col("Birim fiyatı / bedel", ColKind.PRICE, 1.1), Col("Toplam miktar", ColKind.QTY, 1),
            Col("Önceki hakedişteki miktar", ColKind.QTY, 1), Col("Bu hakedişteki miktar", ColKind.QTY, 1),
            Col(f"Toplam tutar ({cur})", ColKind.MONEY, 1.3), Col(f"Önceki hakedişteki tutar ({cur})", ColKind.MONEY, 1.3),
            Col(f"Bu hakedişte ödenecek tutar ({cur})", ColKind.MONEY, 1.3)]
    table = Table(cols, rows, [None, None, "TOPLAM İMALAT TUTARLARI", None, None, None, None, None, r.total_cum,
                               r.total_prev, r.this_total])
    note = Text("Götürü kalemlerde 'birim fiyatı' kalem bedeli, miktarlar gerçekleşme oranıdır (1,00 = %100).", "note")
    return Page("5-Hak.İm.İcm.", "HAKEDİŞ İMALAT İCMALİ", [TableBlock(table, bold_rows=bold), note, signatures(view)],
                header(view, 5), landscape=True)


def taseron_packet(view) -> Packet:
    c = view.contract
    pages = [_cover(view), _payment_sheet(view), _summary(view), _tracking(view), _works(view), green_book(view, 6),
             deductions_page(view, 7), control_page(view, 8)]
    return Packet(f"{c.title} — {view.payment.label}", c.employer.upper(), pages, view.draft,
                  ("TASLAK · " if view.draft else "") + DISCLAIMER)
