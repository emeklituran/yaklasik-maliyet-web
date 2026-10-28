"""Merkezi rapor kataloğunun ikinci bölümü: metraj, birim fiyat listesi, disiplin dağılımı, analiz föyü, malzeme onay listesi,
teslim tutanağı, hakediş icmali, nakit akışı. Yeni hesap kaynağı yoktur; her rapor ilgili servis/kayıttan okur.
`module_reports` sonunda bu liste kataloğa eklenir."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from yapimmaliyet.domain.money import format_price_tr, format_tr, from_db_text
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
from yapimmaliyet.reports.module_reports import Filters, ReportDef, _meta
from yapimmaliyet.services.cost_tracking_service import CostTrackingService
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService
from yapimmaliyet.services.tender_service import TenderService

M, Q, P, PC, I = ColKind.MONEY, ColKind.QTY, ColKind.PRICE, ColKind.PERCENT, ColKind.INT
ZERO = Decimal(0)


def _need_project(f: Filters, what: str) -> int:
    if f.project_id is None:
        raise ValueError(f"{what} için proje seçin.")
    return f.project_id


def _estimates(conn, pid: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, revision FROM estimates WHERE project_id = ? ORDER BY name, revision DESC",
                        (pid,)).fetchall()


def quantity_schedule(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Metraj cetveli")
    sections = []
    for e in _estimates(conn, pid):
        rows = [[r["line_no"], r["raw_code"] or "", r["raw_description"] or "", r["raw_unit"] or "",
                 from_db_text(r["quantity"]), DISCIPLINES.get(r["discipline"] or "", ""), r["notes"] or ""]
                for r in conn.execute("SELECT * FROM estimate_items WHERE estimate_id = ? ORDER BY line_no", (e["id"],))
                if not f.discipline or r["discipline"] == f.discipline]
        sections.append(Section(f"{e['name']} (rev. {e['revision']})",
                                Table([Col("Sıra", I, 0.4), Col("Poz no", width=0.9), Col("İş kalemi", width=3),
                                       Col("Birim", width=0.5), Col("Miktar", Q, 0.9), Col("Disiplin", width=0.8),
                                       Col("Not", width=1.4)], rows)))
    return ReportDoc("Metraj Cetveli", _meta(conn, f), sections or [Section("Keşif listesi yok")],
                     ["Miktarlar keşif kalemlerinin kayıtlı metrajıdır; metraj satırı dökümü hakediş sayfalarındadır."])


def unit_price_list(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Birim fiyat listesi")
    sections = []
    for e in _estimates(conn, pid):
        view = TenderService(conn).view(e["id"])
        rows = [[ln.code or "", ln.description or "", ln.unit or "", ln.ym_unit_price] for ln in view.lines
                if ln.ym_unit_price is not None and (not f.discipline or ln.discipline == f.discipline)]
        sections.append(Section(f"{e['name']} (rev. {e['revision']})",
                                Table([Col("Poz no", width=1), Col("Tanım", width=3.5), Col("Birim", width=0.6),
                                       Col("Birim fiyat", P, 1)], rows)))
    return ReportDoc("Birim Fiyat Listesi (keşifte eşleşen pozlar)", _meta(conn, f), sections or [Section("Keşif listesi yok")],
                     ["Fiyatlar keşif kalemlerinin onaylı eşleşmesinden gelir; eşleşmemiş kalem listelenmez."])


def discipline_distribution(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Disiplin bazlı maliyet dağılımı")
    sections = []
    for e in _estimates(conn, pid):
        view = TenderService(conn).view(e["id"])
        agg: dict[str, list] = {}
        for ln in view.lines:
            a = agg.setdefault(ln.discipline or "", [0, ZERO, 0])
            a[0] += 1
            if ln.ym_amount is None:
                a[2] += 1
            else:
                a[1] += ln.ym_amount
        total = sum((a[1] for a in agg.values()), ZERO)
        rows = [[DISCIPLINES.get(k, "Disiplin belirtilmemiş"), a[0], a[1], (a[1] / total) if total else None, a[2]]
                for k, a in agg.items()]
        sections.append(Section(f"{e['name']} (rev. {e['revision']})",
                                Table([Col("Disiplin", width=2), Col("Kalem", I, 0.6), Col("Yaklaşık maliyet (KDV hariç)", M, 1.4),
                                       Col("Pay", PC, 0.7), Col("Fiyatsız kalem", I, 0.8)], rows,
                                      ["Toplam", sum(a[0] for a in agg.values()), total, None, sum(a[2] for a in agg.values())])))
    return ReportDoc("Disiplin Bazlı Maliyet Dağılımı", _meta(conn, f), sections or [Section("Keşif listesi yok")],
                     ["Tutarlar kalem bazında yaklaşık maliyet birim fiyatlarıyla hesaplanır (genel gider ve kâr hariç)."])


def analysis_sheets(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Analiz föyü")
    ids = [e["id"] for e in _estimates(conn, pid)]
    if not ids:
        raise ValueError("Projede keşif listesi yok.")
    marks = ",".join("?" * len(ids))
    analyses = conn.execute(
        "SELECT DISTINCT oa.* FROM estimate_items i JOIN position_prices pp ON pp.id = i.position_price_id"
        " JOIN positions p ON p.id = pp.position_id JOIN official_analyses oa ON oa.code_norm = p.code_norm"
        f" WHERE i.estimate_id IN ({marks}) ORDER BY oa.code_norm", ids).fetchall()
    sections = []
    for a in analyses:
        items = conn.execute("SELECT * FROM official_analysis_items WHERE analysis_id = ? ORDER BY line_no", (a["id"],)).fetchall()
        rows = [[i["section"] or "", i["code"] or "", i["description"], i["unit"] or "",
                 from_db_text(i["quantity"]) if i["quantity"] else None,
                 from_db_text(i["unit_price"]) if i["unit_price"] else None,
                 from_db_text(i["amount"]) if i["amount"] else None] for i in items]
        paras = [f"Birim: {a['unit'] or '—'} · Ara toplam: {format_tr(from_db_text(a['subtotal'])) if a['subtotal'] else '—'} · "
                 f"Genel gider ve kâr: {format_tr(from_db_text(a['overhead_amount'])) if a['overhead_amount'] else '—'} · "
                 f"Birim fiyat: {format_price_tr(from_db_text(a['unit_price'])) if a['unit_price'] else '—'}"]
        sections.append(Section(f"{a['code']} — {a['name']}",
                                Table([Col("Bölüm", width=0.9), Col("Kod", width=0.9), Col("Bileşen", width=3), Col("Birim", width=0.5),
                                       Col("Miktar", Q, 0.8), Col("Birim fiyat", P, 0.9), Col("Tutar", M, 0.9)], rows), paras))
    return ReportDoc("Analiz Föyü (keşifte eşleşen pozların resmî analizleri)", _meta(conn, f),
                     sections or [Section("Keşifte eşleşen pozlar için içe aktarılmış resmî analiz bulunamadı")],
                     ["Analizler 'Analiz Kitapları' içe aktarımından gelir; program analiz verisi üretmez."])


def material_approvals(conn, f: Filters) -> ReportDoc:
    rows = []
    for r in conn.execute(
            "SELECT rq.*, o.firm, o.revision AS orev, o.id AS oid FROM purchase_requests rq JOIN purchase_offers o ON o.id = rq.selected_offer_id"
            " WHERE rq.status = 'ONAYLANDI' AND (? IS NULL OR rq.project_id = ?) ORDER BY rq.decided_at, rq.id", (f.project_id, f.project_id)):
        if f.party_id and not conn.execute("SELECT 1 FROM purchase_offers WHERE id = ? AND party_id = ?", (r["oid"], f.party_id)).fetchone():
            continue
        if not f.in_range((r["decided_at"] or "")[:10]):
            continue
        for ln in conn.execute("SELECT i.line_no, i.description, i.unit, i.quantity, l.brand_model, l.list_price, l.discount_rate"
                               " FROM purchase_items i LEFT JOIN purchase_offer_lines l ON l.item_id = i.id AND l.offer_id = ?"
                               " WHERE i.request_id = ? ORDER BY i.line_no", (r["oid"], r["id"])):
            net = (from_db_text(ln["list_price"]) * (1 - from_db_text(ln["discount_rate"]) / 100)) if ln["list_price"] else None
            rows.append([r["title"], f"{r['firm']} (rev. {r['orev']})", ln["description"], ln["brand_model"] or "", ln["unit"],
                         from_db_text(ln["quantity"]), net, format_date_tr((r["decided_at"] or "")[:10]), r["approver"] or ""])
    t = Table([Col("Talep", width=1.6), Col("Seçilen teklif", width=1.6), Col("Kalem", width=2), Col("Marka/model", width=1.2),
               Col("Birim", width=0.5), Col("Miktar", Q, 0.8), Col("Net birim fiyat", P, 0.9), Col("Onay tarihi", width=0.8),
               Col("Onaylayan", width=1)], rows)
    return ReportDoc("Malzeme Onay Listesi", _meta(conn, f), [Section("Onaylı satın alma kararları", t)],
                     ["Net birim fiyat: iskonto sonrası, KDV hariç; onaylı kayıt değiştirilemez, revizyon gerekçeyle yapılır."])


def delivery_receipts(conn, f: Filters) -> ReportDoc:
    from yapimmaliyet.services.order_service import OrderService

    sections = []
    for o in OrderService(conn).orders(f.project_id):
        if f.party_id and not conn.execute("SELECT 1 FROM purchase_offers WHERE id = ? AND party_id = ?", (o.offer_id, f.party_id)).fetchone():
            continue
        desc = {ln.id: ln for ln in o.lines}
        rows = []
        for d in o.deliveries:
            if not f.in_range(d.delivery_date):
                continue
            for lid, q in d.quantities.items():
                ln = desc[lid]
                rows.append([format_date_tr(d.delivery_date), d.waybill_no or "", ln.description, ln.unit, q, ln.unit_price,
                             d.received_by or ""])
        if rows:
            sections.append(Section(f"Sipariş {o.order_no} — {o.supplier}",
                                    Table([Col("Teslim tarihi"), Col("İrsaliye"), Col("Kalem", width=2.5), Col("Birim", width=0.5),
                                           Col("Teslim miktarı", Q, 0.9), Col("Net birim fiyat", P, 0.9), Col("Teslim alan")], rows)))
    return ReportDoc("Teslim Tutanağı", _meta(conn, f), sections or [Section("Teslim kaydı yok")],
                     ["Her sipariş için irsaliyeli teslim satırları; imza alanı yazdırılan çıktıda ayrıca doldurulur."])


def payment_summary(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Hakediş icmali")
    pay = ProgressPaymentService(conn)
    rows = []
    for c in conn.execute("SELECT id, title, kind, contractor FROM pp_contracts WHERE project_id = ? ORDER BY kind, id", (pid,)):
        for p, snap in pay.effective_snapshots(c["id"]):
            if not f.in_range(p.period_end):
                continue
            t = snap["totals"]
            rows.append([c["title"], "Taşeron" if c["kind"] == "TASERON" else "İdari", p.label, format_date_tr(p.period_end), p.status,
                         Decimal(str(t["work_period"])),
                         Decimal(str(t["price_diff_period"])), Decimal(str(t["vat"])), Decimal(str(t["deductions_total"])),
                         Decimal(str(t["payable"]))])
    total = lambda i: sum((r[i] for r in rows), ZERO)   # noqa: E731
    tbl = Table([Col("Sözleşme", width=1.8), Col("Tür", width=0.6), Col("Hakediş", width=0.9), Col("Dönem sonu", width=0.8),
                 Col("Durum", width=0.6), Col("Dönem işi", M), Col("Fiyat farkı", M), Col("KDV", M), Col("Kesintiler", M),
                 Col("Ödenecek", M)], rows, ["Toplam", "", "", "", "", total(5), total(6), total(7), total(8), total(9)])
    return ReportDoc("Hakediş İcmali", _meta(conn, f), [Section("Geçerli (onaylı / ödendi) hakedişler", tbl)],
                     ["Yalnız onaylı ve ödendi hakedişler; düzeltilmiş (eski revizyon) hakedişler yer almaz."])


def cash_flow_report(conn, f: Filters) -> ReportDoc:
    pid = _need_project(f, "Nakit akışı")
    cf = CostTrackingService(conn).cash_flow(pid)
    rows = [[r.period, r.inflow, r.outflow, r.net, r.cumulative] for r in cf.rows if f.in_range(r.period + "-01") or
            (not f.date_from and not f.date_to)]
    tbl = Table([Col("Dönem"), Col("Giriş (tahsilat)", M), Col("Çıkış (ödeme)", M), Col("Net", M), Col("Kümülatif", M)], rows,
                ["Toplam", cf.inflow_total, cf.outflow_total, cf.inflow_total - cf.outflow_total, None])
    open_ = Table([Col("Gösterge", width=2.5), Col("Tutar", M)], [["Tahsil edilmemiş onaylı idari hakediş (alacak)", cf.receivable],
                                                                   ["Ödenmemiş gider, teslim ve taşeron hakedişi (borç)", cf.payable]])
    return ReportDoc("Nakit Akışı", _meta(conn, f), [Section("Aylık nakit akışı (KDV dahil, ödeme/tahsilat tarihine göre)", tbl),
                                                     Section("Açık bakiyeler", open_)], list(cf.notes) +
                     ["Nakit akışı maliyet ve hakediş gelirinden farklıdır; yalnız ödeme/tahsilat tarihi girilmiş kayıtları içerir."])


def stock_status(conn, f: Filters) -> ReportDoc:
    from yapimmaliyet.services.stock_service import StockService

    rows = [[r.order_no, r.supplier, r.description, r.unit, r.received, r.issued, r.balance, r.unit_price, r.value,
             format_date_tr(r.last_move)] for r in StockService(conn).balances(f.project_id) if
            (not f.party_id or conn.execute("SELECT 1 FROM po_order_lines l JOIN po_orders o ON o.id = l.order_id JOIN "
                                            "purchase_offers x ON x.id = o.offer_id WHERE l.id = ? AND x.party_id = ?",
                                            (r.line_id, f.party_id)).fetchone())]
    total = sum((r[8] for r in rows), ZERO)
    return ReportDoc("Stok Durum Raporu", _meta(conn, f), [Section("Sipariş kalemi bazında stok", Table(
        [Col("Sipariş"), Col("Tedarikçi", width=1.6), Col("Kalem", width=2.4), Col("Birim", width=0.5),
         Col("Teslim alınan", Q, 0.9), Col("Çıkış", Q, 0.8), Col("Kalan", Q, 0.8), Col("Net birim fiyat", P, 0.9),
         Col("Stok değeri", M, 0.9), Col("Son hareket")], rows, ["Toplam", "", "", "", None, None, None, None, total, ""]))],
                     ["Stok girişi sipariş teslimlerinden gelir; maliyet teslimde tanınır, stok değeri yalnız bilgidir."])


def rate_table(conn, f: Filters) -> ReportDoc:
    from yapimmaliyet.services.rate_service import KINDS, RateService

    svc = RateService(conn)
    rows = [[r.key, r.name, KINDS[r.kind], r.value, r.unit, format_date_tr(r.valid_from), format_date_tr(r.valid_to) or "süresiz",
             r.source_ref, "Doğrulandı" if r.verified else "Doğrulanmadı"] for r in svc.rates()]
    t = Table([Col("Anahtar"), Col("Ad", width=1.6), Col("Tür", width=1.2), Col("Değer", ColKind.PRICE, 0.9), Col("Birim", width=0.5),
               Col("Başlangıç"), Col("Bitiş"), Col("Kaynak", width=1.8), Col("Durum")], rows)
    return ReportDoc("Oran ve Parametre Tablosu", _meta(conn, f), [Section("Oranlar", t)],
                     list(dict.fromkeys(svc.warnings())) + ["Oranlar kullanıcı girişidir; program oran üretmez, kaynak ve doğrulama kayıtlıdır."])


EXTRA = [
    ReportDef("ORAN_TABLOSU", "Oran ve parametre tablosu", "Ayarlar", (), rate_table),
    ReportDef("STOK_DURUM", "Stok durum raporu", "Satın Alma", ("project", "party"), stock_status),
    ReportDef("METRAJ", "Metraj cetveli", "Metraj ve Yaklaşık Maliyet", ("project", "discipline"), quantity_schedule),
    ReportDef("BIRIM_FIYAT_LISTESI", "Birim fiyat listesi (keşifte eşleşen pozlar)", "Metraj ve Yaklaşık Maliyet",
              ("project", "discipline"), unit_price_list),
    ReportDef("DISIPLIN_DAGILIM", "Disiplin bazlı maliyet dağılımı", "Metraj ve Yaklaşık Maliyet", ("project",), discipline_distribution),
    ReportDef("ANALIZ_FOYU", "Analiz föyü (eşleşen pozların resmî analizleri)", "Birim Fiyatlar ve Analizler", ("project",), analysis_sheets),
    ReportDef("MALZEME_ONAY", "Malzeme onay listesi", "Satın Alma", ("project", "dates", "party"), material_approvals),
    ReportDef("TESLIM_TUTANAGI", "Teslim tutanağı", "Satın Alma", ("project", "dates", "party"), delivery_receipts),
    ReportDef("HAKEDIS_ICMAL", "Hakediş icmali", "Hakedişler", ("project", "dates"), payment_summary),
    ReportDef("NAKIT_AKISI", "Nakit akışı", "Maliyet ve İş Programı", ("project", "dates"), cash_flow_report),
]
