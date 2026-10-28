"""Taşeron ve idari hakediş dosyalarının ortak sayfaları: başlık bilgileri, imzalar, yeşil defter (metraj),
kesinti dökümü ve kontrol notları (hesap varsayımları, uyarılar, onay geçmişi, kesinleşmiş kayıt özeti)."""

from __future__ import annotations

from decimal import Decimal

from yapimmaliyet.domain.money import format_tr
from yapimmaliyet.domain.progress import DEDUCTION_BASES, ITEM_SOURCES, PRICE_DIFF_MODES, STATUSES, VAT_BASES
from yapimmaliyet.domain.tender import DISCIPLINES, TENDER_MODELS
from yapimmaliyet.reports.model import Col, ColKind, Table
from yapimmaliyet.reports.packet import KeyValues, Page, Signature, Signatures, TableBlock, Text

ZERO = Decimal(0)
DISCLAIMER = ("Kontrol amaçlı çıktıdır; resmî hakediş formu yerine geçmez. Uygulanacak oran ve kurallar sözleşmeye ve "
              "yürürlükteki mevzuata göre ilgililerce doğrulanmalıdır.")
DEFAULT_SIGNATURES = {
    "TASERON": [("Uzman Ekip / Taşeron", None), ("Saha Mühendisi", ""), ("Şantiye Şefi", "")],
    "IDARI": [("Yüklenici", None), ("Kontrol Mühendisi", ""), ("Kontrol Şefi", ""), ("Kontrol Amiri", ""),
              ("Onaylayan", "")],
}


def d(text: str | None) -> str:
    """ISO tarih → gg.aa.yyyy (boşsa '…')."""
    if not text:
        return "……/……/……"
    y, m, day = text[:10].split("-")
    return f"{day}.{m}.{y}"


def money(v: Decimal | None, currency: str = "TL") -> str:
    return "—" if v is None else f"{format_tr(v)} {currency}"


def pct(v: Decimal | None) -> str:
    return "—" if v is None else "%" + format_tr(v * 100, 2)


def header(view, page_no: int) -> list[tuple[str, str]]:
    p = view.payment
    return [("Hakediş No", str(p.no) + (f" / rev. {p.revision}" if p.revision else "")),
            ("Hakediş Tarihi", d(p.payment_date or p.period_end)), ("Sayfa No", str(page_no))]


def signatures(view) -> Signatures:
    c = view.contract
    sigs = [Signature(s.get("role", ""), s.get("name", ""), s.get("title", "")) for s in c.signatures]
    if not sigs:
        sigs = [Signature(role, c.contractor if name is None else name) for role, name in DEFAULT_SIGNATURES[c.kind]]
    return Signatures(sigs, f"Tarih: {d(view.payment.payment_date or view.payment.period_end)}")


def contract_amount(view):
    """Hesapta kullanılan bedel: değişikliklerle güncel bedel (onaylıda kesinleşmiş kayıttaki bedel)."""
    return view.contract.contract_amount if view.contract_amount is None else view.contract_amount


def contract_rows(view) -> list[tuple[str, str]]:
    c, r = view.contract, view.result
    rows = [("Sözleşme modeli", TENDER_MODELS[c.model]), ("Sözleşme bedeli", money(c.contract_amount, c.currency))]
    if contract_amount(view) != c.contract_amount:
        rows.append(("Değişikliklerle güncel sözleşme bedeli", money(contract_amount(view), c.currency)))
    if c.contract_no or c.contract_date:
        rows.append(("Sözleşme tarihi ve no", f"{d(c.contract_date)}  {c.contract_no or ''}".strip()))
    if c.ikn:
        rows.append(("İKN", c.ikn))
    rows += [("İşe başlama / bitiş tarihi", f"{d(c.start_date)} – {d(c.end_date)}"),
             ("Hakediş dönemi", f"{d(view.payment.period_start)} – {d(view.payment.period_end)}"),
             ("Verilen avans toplamı", money(c.advance_total, c.currency)),
             ("Mahsubu yapılan avans (kümülatif)", money(r.advance_cum, c.currency)),
             ("Gerçekleşme (kümülatif iş / sözleşme bedeli)", pct(r.progress)),
             ("Kalan sözleşme tutarı", money(r.remaining_contract, c.currency))]
    return rows


def deduction_label(t) -> str:
    if t.base == "MANUEL":
        return t.name
    rate = format_tr(t.rate * 100, 3).rstrip("0").rstrip(",")
    base = {"BU_HAKEDIS": "bu hakediş", "BU_HAKEDIS_EKSI_AVANS": "bu hakediş − avans", "KDV": "KDV"}[t.base]
    return f"{t.name} ({base} × %{rate})"


def green_book(view, page_no: int) -> Page:
    """Yeşil defter: her kalem için önceki hakedişten, metraj cetvelinden (bu dönem) ve toplam miktar."""
    rows, bold = [], []
    for ln in view.result.lines:
        it = ln.item
        if not (ln.input.prev_qty or ln.input.period_qty):
            continue
        unit = "oran" if it.method == "GOTURU" else (it.unit or "")
        bold.append(len(rows))
        rows.append([it.line_no, it.code, f"{it.description} ({unit})", None, None])
        rows.append([None, None, "Önceki hakedişten", ln.input.prev_qty, None])
        meas = view.measurements.get(it.id, [])
        for m in meas:
            ref = f" [{m['ref']}]" if m.get("ref") else ""
            rows.append([None, None, f"  {m['description']}{ref}", Decimal(m["quantity"]), None])
        if not meas:
            rows.append([None, None, "Bu dönem (metraj cetvelinden)", ln.input.period_qty, None])
        rows.append([None, None, "Toplam", None, ln.cum_qty])
    cols = [Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("Yapılan işin açıklaması", width=5),
            Col("Miktar", ColKind.QTY, 1.2), Col("Toplam", ColKind.QTY, 1.2)]
    blocks = [TableBlock(Table(cols, rows), bold_rows=tuple(bold))] if rows else \
        [Text("Bu hakedişte ve öncekilerde miktar girilmiş kalem yok.", "note")]
    blocks.append(Text("Götürü kalemlerde miktar, kalem bedelinin gerçekleşme oranıdır (1,000 = %100).", "note"))
    return Page("Yeşil Defter", "YEŞİL DEFTER (METRAJ)", blocks + [signatures(view)], header(view, page_no))


def deductions_page(view, page_no: int, title: str = "KESİNTİ VE MAHSUP DÖKÜMÜ") -> Page:
    r, cur = view.result, view.contract.currency
    rows, bold = [], []
    for dr in r.deductions:
        bold.append(len(rows))
        rows.append([deduction_label(dr.type), DEDUCTION_BASES[dr.type.base], dr.base_amount, dr.amount])
        for e in dr.entries:
            rows.append([f"  {e.description or '(açıklamasız)'}", None, None, e.amount])
    table = Table([Col("Kesinti", width=4), Col("Hesap", width=2.4), Col(f"Taban ({cur})", ColKind.MONEY, 1.4),
                   Col(f"Tutar ({cur})", ColKind.MONEY, 1.4)], rows,
                  [f"KESİNTİLER TOPLAMI", None, None, r.deductions_total])
    notes = [Text("Kesinti türleri, oranları ve tabanları sözleşme ayarıdır (programda sabit oran yoktur).", "note")]
    if not rows:
        notes.insert(0, Text("Bu sözleşmede etkin kesinti türü tanımlı değil.", "warning"))
    return Page("Kesintiler", title, [TableBlock(table, bold_rows=tuple(bold))] + notes + [signatures(view)],
                header(view, page_no))


def control_page(view, page_no: int) -> Page:
    """Hesabın hangi sözleşme, fiyat, miktar, kesinti ayarı ve varsayımla üretildiği; uyarılar ve onay geçmişi."""
    c, p, r = view.contract, view.payment, view.result
    state = STATUSES[p.status] + (" — kesinleşmiş kayıttan üretildi" if view.from_snapshot else
                                  " — canlı veriden üretildi (TASLAK)")
    kv = [("Durum", state), ("KDV", f"%{format_tr(c.vat_rate * 100, 2)} — {VAT_BASES[c.vat_base]}"),
          ("Fiyat farkı", PRICE_DIFF_MODES[p.price_diff_mode] + (f" (katsayı {r.price_diff_coef})"
                                                                if r.price_diff_coef is not None else "")),
          ("Para birimi", c.currency), ("Yuvarlama", "Satır ve kesinti tutarları kuruşa (ROUND_HALF_UP); toplamlar "
                                                     "yuvarlanmış satırların toplamıdır.")]
    if p.price_diff_note:
        kv.append(("Fiyat farkı açıklaması", p.price_diff_note))
    if p.snapshot_sha:
        kv.append(("Kesinleşmiş kayıt özeti (SHA-256)", p.snapshot_sha[:16] + "…"))
    blocks = [KeyValues(kv)]
    sources = [[it.line_no, it.code, ITEM_SOURCES.get(it.source, it.source),
                DISCIPLINES.get(it.discipline or "", "—"), it.group_name or ""] for it in view.items
               if it.source != "SOZLESME"]
    if sources:
        blocks.append(TableBlock(Table([Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("Kaynak", width=2),
                                        Col("Disiplin", width=1.3), Col("Grup", width=2)], sources),
                                 "Sözleşme eki dışındaki kalemler"))
    warn = [[w.level, w.message] for w in r.warnings]
    blocks.append(TableBlock(Table([Col("Düzey", width=0.8), Col("Uyarı / denetim sonucu", width=8)],
                                   warn or [["—", "Uyarı yok."]]), "Denetimler"))
    if view.status_log:
        blocks.append(TableBlock(Table([Col("Zaman", width=1.6), Col("Geçiş", width=2), Col("Kullanıcı", width=1.5),
                                        Col("Not", width=3.5)],
                                       [[e["at"][:16].replace("T", " "), f"{STATUSES.get(e['from_status'] or '', '—')}"
                                         f" → {STATUSES.get(e['to_status'], e['to_status'])}", e["user_name"],
                                         e["note"] or ""] for e in view.status_log]), "Kontrol ve onay geçmişi"))
    blocks.append(Text(DISCLAIMER, "warning"))
    return Page("Kontrol Notları", "HESAP VARSAYIMLARI VE KONTROL NOTLARI", blocks, header(view, page_no))


def amount(s: str | None) -> Decimal:
    return Decimal(s) if s not in (None, "") else ZERO
