"""İhale teklifleri: karşılaştırma raporu ve modele göre teklif cetveli (boş form ya da doldurulmuş teklif)."""

from __future__ import annotations

from decimal import Decimal

from yapimmaliyet.domain.money import format_tr
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.tender import DISCIPLINES, PRICING_METHODS, UNASSIGNED, BidComparison
from yapimmaliyet.domain.tr_words import amount_in_words
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table


def _status(e) -> str:
    if e.complete:
        return "Tam"
    return f"Eksik ({len(e.missing)} kalem)" if e.missing else "Eksik (tutar yok)"


def bid_comparison(view, cmp: BidComparison, app_version: str) -> ReportDoc:
    p, est = view.project, view.estimate
    meta = [("Proje", p.name)]
    if p.ikn:
        meta.append(("İKN", p.ikn))
    if p.authority_name:
        meta.append(("İdare", p.authority_name))
    meta += [("Keşif", est.display_name), ("İhale modeli", view.summary.model_label),
             ("Yaklaşık maliyet (KDV hariç)",
              "hesaplanamadı — fiyatsız kalem var" if cmp.ym_total is None else f"{format_tr(cmp.ym_total)} TL"),
             ("Uygulama sürümü", app_version)]
    names = [e.bid.bidder + (" (ÖRNEK)" if e.bid.is_sample else "") for e in cmp.rows]
    summary = Table([Col("İstekli", width=2.5), Col("Teklif tutarı (TL, KDV hariç)", ColKind.MONEY, 1.6),
                     Col("Yaklaşık maliyete fark (TL)", ColKind.MONEY, 1.6), Col("Oran", ColKind.PERCENT, 1),
                     Col("Sıra (bilgi)", ColKind.INT, 0.8), Col("Durum", width=1.4)],
                    [[n, e.total, e.diff, e.ratio, e.rank, _status(e)] for n, e in zip(names, cmp.rows)])
    sections = [Section("Teklif özeti", summary)]
    if cmp.model != "ANAHTAR_TESLIM" and cmp.rows:
        cols = [Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.2), Col("İmalat", width=3),
                Col("Birim", width=0.6), Col("Miktar", ColKind.QTY, 0.9), Col("Yöntem", width=1),
                Col("YM tutarı", ColKind.MONEY, 1.2)] + [Col(n, ColKind.MONEY, 1.2) for n in names]
        rows = [[ln.line_no, ln.code, ln.description, ln.unit, ln.quantity,
                 PRICING_METHODS.get(ln.method or "", UNASSIGNED), ln.ym_amount]
                + [e.amounts.get(ln.item_id) for e in cmp.rows] for ln in view.lines]
        total = [None, None, "TOPLAM", None, None, None, cmp.ym_total] + [e.total for e in cmp.rows]
        sections.append(Section("Kalem bazında teklif tutarları", Table(cols, rows, total)))
        disc_rows = []
        for key, label in list(DISCIPLINES.items()) + [(None, UNASSIGNED)]:
            ids = [ln.item_id for ln in view.lines if ln.discipline == key]
            if not ids:
                continue
            ym = sum((ln.ym_amount or Decimal(0) for ln in view.lines if ln.discipline == key), Decimal(0))
            disc_rows.append([label, len(ids), ym] + [sum((e.amounts.get(i) or Decimal(0) for i in ids), Decimal(0))
                                                      for e in cmp.rows])
        sections.append(Section("Disipline göre tutarlar (eksik kalemler sıfır değil, hariç tutulmuştur)", Table(
            [Col("Disiplin", width=2), Col("Kalem", ColKind.INT, 0.7), Col("YM tutarı", ColKind.MONEY, 1.3)]
            + [Col(n, ColKind.MONEY, 1.3) for n in names], disc_rows)))
    notes = list(cmp.notes) + [n for n in view.summary.notes if n not in cmp.notes]
    if any(e.bid.is_sample for e in cmp.rows):
        notes.append("'(ÖRNEK)' işaretli teklifler sentetik test verisidir; gerçek istekli teklifi değildir.")
    return ReportDoc("İhale Teklifleri Karşılaştırması", meta, sections, notes, draft=False)


SCHEDULE_TITLES = {"BIRIM_FIYAT": "Birim Fiyat Teklif Cetveli",
                   "ANAHTAR_TESLIM": "Teklif Bedeli Özeti — Anahtar Teslim Götürü Bedel",
                   "KARMA": "Teklif Cetveli — Karma (Götürü Bedel Kısmı ve Birim Fiyat Kısmı)"}
SIGNATURE = ["İsteklinin adı soyadı / ticaret unvanı: ..............................................",
             "Tarih: ....../....../..........          Kaşe ve imza: ..............................................."]
FORM_NOTE = ("Bu cetvel YapımMaliyet'in ürettiği bir taslaktır. İdareye sunulacak teklifte ihale dokümanındaki standart "
             "form (Kamu İhale Kurumu standart formları ve idarenin eki) esas alınmalıdır. Tutarlar KDV hariçtir.")


def offer_schedule(view, app_version: str, bid=None, lines: dict | None = None) -> ReportDoc:
    """Modele göre teklif cetveli. `bid` verilmezse istekliye verilecek BOŞ form (ihale dokümanı eki) üretilir;
    boş formda yaklaşık maliyet ve idare fiyatları yer almaz (4734 md. 9: yaklaşık maliyet dokümanda açıklanmaz)."""
    p, est, model = view.project, view.estimate, view.project.tender_model
    if model == "KARMA" and any(ln.method is None for ln in view.lines):
        raise ValueError("Karma modelde teklif yöntemi atanmamış kalemler var; teklif cetveli üretilemez "
                         "(Keşif Listeleri → Disiplin / Teklif Yöntemi).")
    lines = lines or {}
    blank = bid is None
    meta = [("İşin adı", p.name)] + ([("İKN", p.ikn)] if p.ikn else []) + \
        ([("İdare", p.authority_name)] if p.authority_name else []) + \
        [("Keşif", est.display_name), ("İhale modeli", view.summary.model_label),
         ("Belge", "BOŞ FORM — ihale dokümanı eki; fiyatları istekli doldurur" if blank
          else f"Teklif — {bid.bidder}" + (" (ÖRNEK / sentetik)" if bid.is_sample else "")),
         ("Uygulama sürümü", app_version)]
    amounts = {}
    for ln in view.lines:
        up, lump = lines.get(ln.item_id, (None, None))
        amounts[ln.item_id] = (up, quantize(ln.quantity * up, 2) if up is not None else lump)
    bf = [ln for ln in view.lines if ln.method == "BIRIM_FIYAT"]
    gb = [ln for ln in view.lines if ln.method == "GOTURU"]
    sections, total = [], None

    def unit_table(rows) -> Table:
        cols = [Col("Sıra No", ColKind.INT, 0.5), Col("İş Kalemi No", width=1.2),
                Col("İş Kaleminin Adı ve Kısa Açıklaması", width=4), Col("Birimi", width=0.6),
                Col("Miktarı", ColKind.QTY, 0.9), Col("Teklif Edilen Birim Fiyat (TL)", ColKind.PRICE, 1.3),
                Col("Tutarı (TL)", ColKind.MONEY, 1.3)]
        sub = None if blank else sum((amounts[ln.item_id][1] or Decimal(0) for ln in rows), Decimal(0))
        return Table(cols, [[i, ln.code, ln.description, ln.unit, ln.quantity, amounts[ln.item_id][0],
                             amounts[ln.item_id][1]] for i, ln in enumerate(rows, start=1)],
                     [None, None, "TOPLAM", None, None, None, sub])

    if model == "ANAHTAR_TESLIM":
        total = None if blank else bid.lump_total
        sections.append(Section("Teklif bedeli", Table(
            [Col("Açıklama", width=4), Col("Tutar (TL, KDV hariç)", ColKind.MONEY, 1.5)],
            [["İşin tamamı için teklif edilen bedel (anahtar teslim götürü bedel)", total]])))
    elif model == "BIRIM_FIYAT":
        t = unit_table(bf)
        total = t.total_row[-1]
        sections.append(Section("Birim fiyat teklif cetveli", t))
    else:
        cols = [Col("Sıra No", ColKind.INT, 0.5), Col("İş Kalemi No", width=1.2),
                Col("İş Kaleminin / Grubunun Adı", width=4.6), Col("Teklif Edilen Tutar (TL)", ColKind.MONEY, 1.5)]
        a_sum = None if blank else sum((amounts[ln.item_id][1] or Decimal(0) for ln in gb), Decimal(0))
        sections.append(Section("A — Götürü bedel kısmı", Table(
            cols, [[i, ln.code, ln.description, amounts[ln.item_id][1]] for i, ln in enumerate(gb, start=1)],
            [None, None, "A TOPLAMI", a_sum])))
        t = unit_table(bf)
        sections.append(Section("B — Birim fiyat kısmı", t))
        total = None if blank else a_sum + t.total_row[-1]
        sections.append(Section("Teklif toplamı", Table(
            [Col("Kısım", width=4), Col("Tutar (TL, KDV hariç)", ColKind.MONEY, 1.5)],
            [["A — Götürü bedel kısmı", a_sum], ["B — Birim fiyat kısmı", t.total_row[-1]],
             ["TOPLAM TEKLİF BEDELİ", total]])))
    missing = [] if blank else [ln.line_no for ln in view.lines if model != "ANAHTAR_TESLIM"
                                and amounts[ln.item_id][1] is None]
    words = ("Toplam teklif bedeli (KDV hariç): ........................................ TL "
             "(yazıyla: ...................................................................)") if blank or total is None \
        else f"Toplam teklif bedeli (KDV hariç): {format_tr(total)} TL — yazıyla: {amount_in_words(total)}."
    sections.append(Section("Beyan ve imza", paragraphs=[words, *SIGNATURE]))
    notes = [FORM_NOTE] + ([f"Teklif fiyatı girilmemiş kalemler (sıra): {', '.join(map(str, missing))}. Bu teklif "
                            "eksiktir; toplam yalnızca girilen kalemleri içerir."] if missing else [])
    if bid is not None and bid.is_sample:
        notes.append("Bu teklif sentetik örnek veridir; gerçek bir istekli teklifi değildir.")
    return ReportDoc(SCHEDULE_TITLES[model], meta, sections, notes, draft=False, landscape=model != "ANAHTAR_TESLIM")
