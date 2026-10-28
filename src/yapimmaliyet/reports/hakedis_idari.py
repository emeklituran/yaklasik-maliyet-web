"""İdari hakediş (yüklenici → idare) dosyası. Belge sırası ve sütunlar kullanıcının örneğindeki düzenle aynıdır
(Silopi, "13 Nolu Hakediş Raporu"): Dizi Pusulası, Kapak, Hakediş Raporu, Hakediş İcmali, Hakediş Özeti, Yapılan
İşler Listesi (disiplin ağırlıklı), ayrıntılı işler listesi, yeşil defter, fiyat farkı icmali, kesinti ve ödeme özeti,
kontrol notları.

Örnekteki "M.Y.H.B.Y. Örnek No" form numaraları kopyalanmaz: çıktı resmî formun doğrulanmış karşılığı değildir.
Ödenek dilimleri ve fiyat farkı endeks hesap tabloları bu sürümde üretilmez (kontrol notunda belirtilir).
"""

from __future__ import annotations

from decimal import Decimal

from yapimmaliyet.domain.progress import ZERO, group_totals
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.reports.hakedis_common import (
    DISCLAIMER,
    amount,
    contract_amount,
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

DOCS = ["Dizi Pusulası", "Hakediş Raporu Kapağı", "Hakediş Raporu", "Hakediş İcmali", "Hakediş Özeti",
        "Yapılan İşler Listesi", "Yapılan İşler Listesi (ayrıntılı)", "Yeşil Defter (metraj)", "Fiyat Farkı İcmali",
        "Kesinti ve Mahsup Dökümü", "Ödeme Özeti", "Hesap Varsayımları ve Kontrol Notları"]


def _disc(it) -> str:
    return DISCIPLINES.get(it.discipline or "", "Disiplin belirtilmemiş")


def _index(view) -> Page:
    rows = [[i, name, 1, i] for i, name in enumerate(DOCS, start=1)]
    blocks = [TableBlock(Table([Col("Sıra No", ColKind.INT, 0.6), Col("Belgenin cinsi", width=5),
                                Col("Adedi", ColKind.INT, 0.6), Col("Sayfa No", ColKind.INT, 0.7)], rows)),
              Text("Ek belgeler (tutanaklar, SGK dökümleri, sigorta poliçesi, fotoğraflar vb.) programda üretilmez; "
                   "idarenin istediği ekler ayrıca eklenmelidir.", "note")]
    return Page("Dizi Pusulası", f"DİZİ PUSULASI ({view.payment.no} NOLU HAKEDİŞ RAPORU)", blocks, header(view, 1))


def _cover(view) -> Page:
    c, p = view.contract, view.payment
    kv = [("Tarihi", d(p.payment_date or p.period_end)), ("Hakediş no", str(p.no)),
          ("Uygulama yılı", (p.period_end or "")[:4]), ("Yapılan işin adı", c.title), ("İdare", c.employer),
          ("Yüklenicinin adı / ticari unvanı", c.contractor)] + contract_rows(view)
    return Page("1-Kapak", "HAKEDİŞ RAPORU", [KeyValues(kv), signatures(view)], header(view, 2))


def _report(view) -> Page:
    r, p, c = view.result, view.payment, view.contract
    rows = [["A", "Sözleşme fiyatları ile yapılan iş", r.work_cum], ["B", "Fiyat farkı tutarı", r.price_diff_cum],
            ["C", "Toplam tutar (A + B)", r.total_cum], ["D", "Bir önceki hakedişin toplam tutarı", r.total_prev],
            ["E", "Bu hakedişin tutarı (C − D)", r.this_total]]
    if c.vat_base == "NET":
        rows.append(["", "Kesintiler sonrası tutar (KDV tabanı)", r.net_before_vat])
    rows += [["F", f"KDV (%{c.vat_rate * 100:.0f})", r.vat], ["G", "Tahakkuk tutarı (E + F)", r.this_total + r.vat],
             ["", "KESİNTİLER VE MAHSUPLAR", None]]
    bold = [2, 4, len(rows) - 2, len(rows) - 1]
    letters = "abcdefghijklmnoprs"
    for i, dr in enumerate(r.deductions):
        rows.append(["", f"{letters[i % len(letters)]}) {deduction_label(dr.type)}", dr.amount])
    rows.append(["H", "Kesintiler ve mahsuplar toplamı", r.deductions_total])
    bold.append(len(rows) - 1)
    table = Table([Col("", width=0.4), Col(f"{d(p.period_end)} tarihine kadar yapılan işin", width=6),
                   Col(f"Tutar ({c.currency})", ColKind.MONEY, 1.8)], rows,
                  ["I", "Yükleniciye ödenecek tutar (G − H)", r.payable])
    return Page("2-Hakediş Raporu", "HAKEDİŞ RAPORU", [TableBlock(table, bold_rows=tuple(bold)), signatures(view)],
                header(view, 3))


def _icmal(view) -> Page:
    r, cur = view.result, view.contract.currency
    groups = group_totals(r, _disc)
    rows = [[i, g[0].upper() + " İMALATI", g[4], None, g[4]] for i, g in enumerate(groups, start=1)]
    rows.append([len(rows) + 1, "FİYAT FARKI", None, r.price_diff_cum, r.price_diff_cum])
    table = Table([Col("Sıra No", ColKind.INT, 0.6), Col("İşin cinsi", width=4), Col(f"İş tutarı (A) ({cur})",
                                                                                     ColKind.MONEY, 1.7),
                   Col(f"Fiyat farkı tutarı (B) ({cur})", ColKind.MONEY, 1.7), Col(f"Toplam (A+B) ({cur})",
                                                                                  ColKind.MONEY, 1.7)], rows,
                  ["A", "GENEL TOPLAM", r.work_cum, r.price_diff_cum, r.total_cum])
    return Page("3-Hakediş İcmali", "HAKEDİŞ İCMALİ", [TableBlock(table), signatures(view)], header(view, 4))


def _overview(view) -> Page:
    """Hakediş özeti: tüm hakedişlerin kümülatif ve dönem tutarları."""
    cur = view.contract.currency
    rows, prev_cum, tot = [], ZERO, [ZERO] * 4
    adv_names = {dr.type.name for dr in view.result.deductions if dr.type.is_advance}
    for h in view.timeline:
        cum = amount(h.get("work_cum"))
        adv = sum((amount(v) for k, v in h.get("deductions", {}).items() if k in adv_names), ZERO)
        vals = [amount(h["work_period"]), adv, amount(h["price_diff"]), amount(h["work_period"]) + amount(h["price_diff"])]
        tot = [a + b for a, b in zip(tot, vals)]
        rows.append([d(h.get("payment_date") or h.get("period_end")), h["no"], cum, prev_cum] + vals)
        prev_cum = cum
    cols = [Col("Hakediş tarihi", width=1), Col("No", ColKind.INT, 0.5), Col(f"Toplam hakediş tutarı ({cur})",
                                                                             ColKind.MONEY, 1.4),
            Col("Bir önceki hakediş tutarı (kümülatif)", ColKind.MONEY, 1.4), Col("Dönem hakediş tutarı (A)",
                                                                                ColKind.MONEY, 1.3),
            Col("Avans kesintisi", ColKind.MONEY, 1.1), Col("Dönem fiyat farkı", ColKind.MONEY, 1.2),
            Col("Toplam hakediş tutarı (dönem)", ColKind.MONEY, 1.3)]
    return Page("4-Hakediş Özeti", "HAKEDİŞ ÖZETİ", [TableBlock(Table(cols, rows, ["TOPLAM", None, None, None] + tot)),
                                                     signatures(view)], header(view, 5), landscape=True)


def _work_list(view) -> Page:
    """Yapılan işler listesi (disiplin / iş programı grubu ağırlıklı, örnekteki (5)İşler Listesi)."""
    r, c = view.result, view.contract
    total_amount = contract_amount(view)
    rows = []
    for i, (name, contract, prev, period, cum) in enumerate(group_totals(r, _disc), start=1):
        share = contract / total_amount if total_amount else None
        rows.append([i, share, name.upper() + " İMALATI", contract, cum / contract if contract else None, cum,
                     prev / contract if contract else None, prev, period / contract if contract else None, period])
    total_c = sum((row[3] for row in rows), ZERO)
    cols = [Col("Sıra", ColKind.INT, 0.4), Col("İş programı grubu %si", ColKind.PERCENT, 0.9),
            Col("Yapılan iş", width=2.2), Col("Sözleşme bedeli (A)", ColKind.MONEY, 1.3),
            Col("Gerçekleşen toplam imalat %si (B)", ColKind.PERCENT, 0.9), Col("Toplam imalat tutarı (C)",
                                                                              ColKind.MONEY, 1.3),
            Col("Önceki hakediş toplam %si (D)", ColKind.PERCENT, 0.9), Col("Önceki hakediş tutarı (E)",
                                                                          ColKind.MONEY, 1.3),
            Col("Bu hakediş %si (F)", ColKind.PERCENT, 0.8), Col("Bu hakediş imalat tutarı (G)", ColKind.MONEY, 1.3)]
    total = [None, total_c / total_amount if total_amount else None, "TOPLAM", total_c,
             r.work_cum / total_c if total_c else None, r.work_cum, r.work_prev / total_c if total_c else None,
             r.work_prev, r.work_period / total_c if total_c else None, r.work_period]
    blocks = [TableBlock(Table(cols, rows, total))]
    if total_c and abs(total_c - total_amount) >= Decimal("0.01"):
        blocks.append(Text(f"Kalemlerin sözleşme tutarları toplamı ({money(total_c, c.currency)}) sözleşme bedeline "
                           f"({money(total_amount, c.currency)}) eşit değil.", "warning"))
    return Page("5-İşler Listesi", "YAPILAN İŞLER LİSTESİ", blocks + [signatures(view)], header(view, 6),
                landscape=True)


def _detail(view) -> Page:
    """Ayrıntılı işler listesi: disiplin → grup → kalem; götürü kalemde oran, birim fiyatlı kalemde miktar."""
    c, cur = view.contract, view.contract.currency
    total_amount = contract_amount(view)
    rows, bold, key = [], [], None
    for ln in sorted(view.result.lines, key=lambda x: (list(DISCIPLINES).index(x.item.discipline)
                                                       if x.item.discipline in DISCIPLINES else 9, x.item.line_no)):
        it = ln.item
        k = (_disc(it), it.group_name or "")
        if k != key:
            key = k
            bold.append(len(rows))
            rows.append([None, None, None, " / ".join(x for x in k if x).upper()] + [None] * 7)
        contract = it.lump_amount if it.method == "GOTURU" else (it.contract_qty or ZERO) * (it.unit_price or ZERO)
        weight = contract / total_amount if total_amount and contract is not None else None
        rows.append([it.line_no, it.code, weight, it.description + ("" if it.method == "GOTURU" else f" ({it.unit})"),
                     contract, ln.cum_qty, ln.cum_amount, ln.input.prev_qty, ln.prev_amount, ln.input.period_qty,
                     ln.period_amount])
    cols = [Col("Sıra", ColKind.INT, 0.4), Col("Grup kodu / poz", width=1), Col("Grup %si", ColKind.PERCENT, 0.7),
            Col("Yapılan iş", width=2.8), Col(f"Sözleşme tutarı ({cur})", ColKind.MONEY, 1.2),
            Col("Gerçekleşen toplam (oran/miktar)", ColKind.QTY, 0.9), Col("Toplam imalat tutarı", ColKind.MONEY, 1.2),
            Col("Önceki (oran/miktar)", ColKind.QTY, 0.9), Col("Önceki hakediş tutarı", ColKind.MONEY, 1.2),
            Col("Bu hakediş (oran/miktar)", ColKind.QTY, 0.9), Col("Bu hakediş tutarı", ColKind.MONEY, 1.2)]
    r = view.result
    table = Table(cols, rows, [None, None, None, "TOPLAM", None, None, r.work_cum, None, r.work_prev, None,
                               r.work_period])
    return Page("6-Ayrıntılı Liste", "YAPILAN İŞLER LİSTESİ (AYRINTILI)",
                [TableBlock(table, bold_rows=tuple(bold)), signatures(view)], header(view, 7), landscape=True)


def _price_diff(view) -> Page:
    r, p, cur = view.result, view.payment, view.contract.currency
    rows, paid = [], ZERO
    for h in view.timeline:
        ff = amount(h.get("price_diff"))
        rows.append([h["no"], d(h.get("period_start")) + " – " + d(h.get("period_end")), amount(h["work_period"]), ff])
        paid += ff
    blocks = [TableBlock(Table([Col("Hakediş no", ColKind.INT, 0.7), Col("Dönem", width=2),
                                Col(f"Fiyat farkına esas tutar (An) ({cur})", ColKind.MONEY, 1.6),
                                Col(f"Fiyat farkı tutarı ({cur})", ColKind.MONEY, 1.5)], rows,
                               [None, "TOPLAM", None, paid]))]
    if p.price_diff_mode == "KATSAYI":
        blocks.append(KeyValues([("Pn", str(p.price_diff_params.get("pn"))), ("B", str(p.price_diff_params.get("b"))),
                                 ("Katsayı B × (Pn − 1)", str(r.price_diff_coef))]))
    if p.price_diff_note:
        blocks.append(Text(p.price_diff_note, "note"))
    blocks.append(Text("Endeks hesap tabloları bu sürümde üretilmez; fiyat farkı tutarı veya Pn değeri sözleşmedeki "
                       "formül ve yayımlanan endekslerle ayrıca hesaplanıp girilir.", "note"))
    return Page("8-Fiyat Farkı İcmali", "FİYAT FARKI İCMALİ", blocks + [signatures(view)], header(view, 9))


def _payment_overview(view) -> Page:
    r, c = view.result, view.contract
    paid_before = sum((amount(h["payable"]) for h in view.history), ZERO)
    kv = [("Bu hakediş tutarı (E)", money(r.this_total, c.currency)), ("KDV (F)", money(r.vat, c.currency)),
          ("Tahakkuk (E + F)", money(r.this_total + r.vat, c.currency)),
          ("Kesintiler ve mahsuplar (H)", money(r.deductions_total, c.currency)),
          ("Yükleniciye ödenecek (I)", money(r.payable, c.currency)),
          ("Önceki hakedişlerde ödenecek tutarlar toplamı", money(paid_before, c.currency)),
          ("Sözleşme bedeli" + ("" if contract_amount(view) == c.contract_amount else " (değişikliklerle güncel)"),
           money(contract_amount(view), c.currency)),
          ("Kümülatif iş tutarı (sözleşme fiyatlarıyla)", money(r.work_cum, c.currency)),
          ("Kalan iş tutarı", money(r.remaining_contract, c.currency))]
    guarantees = [dr for dr in r.deductions if dr.type.is_guarantee]
    kv += [(f"{g.type.name} (bu hakediş)", money(g.amount, c.currency)) for g in guarantees]
    return Page("10-Ödeme Özeti", "ÖDEME ÖZETİ", [KeyValues(kv), signatures(view)], header(view, 11))


def idari_packet(view) -> Packet:
    c = view.contract
    pages = [_index(view), _cover(view), _report(view), _icmal(view), _overview(view), _work_list(view),
             _detail(view), green_book(view, 8), _price_diff(view), deductions_page(view, 10),
             _payment_overview(view), control_page(view, 12)]
    pages[7].sheet = "7-Yeşil Defter"
    pages[9].sheet = "9-Kesintiler"
    return Packet(f"{c.title} — {view.payment.label}", c.employer.upper(), pages, view.draft,
                  ("TASLAK · " if view.draft else "") + DISCLAIMER)
