"""Merkezi rapor kataloğu: proje, firma, teklif, sözleşme, satın alma, finans ve maliyet/iş programı raporları.

Bu modül yeni bir hesap kaynağı değildir: her rapor ilgili modülün servisinden ya da kayıtlı verisinden okur
(ContractService, SubcontractService, ActivityPlanService, CariService…); burada yalnız rapor modeli kurulur.
Süzgeçler (proje, tarih aralığı, firma, disiplin) uygulanabildiği raporlarda uygulanır; uygulanmayanlar `uses` ile belirtilir.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.domain.money import format_price_tr, format_tr
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
from yapimmaliyet.services.activity_plan_service import ActivityPlanService
from yapimmaliyet.services.cari_service import KINDS as CARI_KINDS
from yapimmaliyet.services.cari_service import CariService
from yapimmaliyet.services.contract_service import ContractService
from yapimmaliyet.services.debt_service import DebtService
from yapimmaliyet.services.firm_service import BID_STATUS, FirmService
from yapimmaliyet.services.note_service import DIRECTIONS, STATUSES as NOTE_STATUSES, NoteService
from yapimmaliyet.services.order_service import OrderService
from yapimmaliyet.services.pipeline_service import PipelineService
from yapimmaliyet.services.purchase_service import PurchaseService
from yapimmaliyet.services.subcontract_service import PACKAGE_STATUSES, SubcontractService
from yapimmaliyet.services.treasury_service import TreasuryService

M, Q, P, PC, I = ColKind.MONEY, ColKind.QTY, ColKind.PRICE, ColKind.PERCENT, ColKind.INT
ZERO = Decimal(0)


@dataclass(frozen=True)
class Filters:
    project_id: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    party_id: int | None = None
    discipline: str | None = None

    def in_range(self, iso: str | None) -> bool:
        if not iso:
            return not (self.date_from or self.date_to)
        return (not self.date_from or iso >= self.date_from) and (not self.date_to or iso <= self.date_to)


@dataclass(frozen=True)
class ReportDef:
    key: str
    label: str
    group: str
    uses: tuple[str, ...]            # uygulanan süzgeçler: project, dates, party, discipline
    build: Callable[[sqlite3.Connection, Filters], ReportDoc]


def _meta(conn: sqlite3.Connection, f: Filters, *extra: tuple[str, str]) -> list[tuple[str, str]]:
    out = list(extra)
    if f.project_id:
        r = conn.execute("SELECT name FROM projects WHERE id = ?", (f.project_id,)).fetchone()
        out.append(("Proje", r["name"] if r else "—"))
    if f.party_id:
        r = conn.execute("SELECT name FROM parties WHERE id = ?", (f.party_id,)).fetchone()
        out.append(("Firma", r["name"] if r else "—"))
    if f.discipline:
        out.append(("Disiplin", DISCIPLINES.get(f.discipline, f.discipline)))
    if f.date_from or f.date_to:
        out.append(("Tarih aralığı", f"{format_date_tr(f.date_from) or '…'} – {format_date_tr(f.date_to) or '…'}"))
    return out + [("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")), ("Uygulama sürümü", __version__)]


def _name(conn, table: str, pid) -> str:
    r = conn.execute(f"SELECT name FROM {table} WHERE id = ?", (pid,)).fetchone()
    return r["name"] if r else ""


# --- Projeler ve ihaleler ---------------------------------------------------------------------
def tender_list(conn, f: Filters) -> ReportDoc:
    rows = [r for r in PipelineService(conn).tenders() if f.in_range(r.tender_date) and
            (f.project_id is None or r.id == f.project_id)]
    t = Table([Col("Proje / ihale", width=2.4), Col("İdare", width=1.6), Col("İKN", width=1.0), Col("İhale tarihi", width=0.9),
               Col("Aşama", width=1.3), Col("Kazanma", width=0.9), Col("Not", width=1.8)],
              [[r.name, r.authority or "", r.ikn or "", format_date_tr(r.tender_date), r.tender_label,
                format_date_tr(r.awarded_date), r.note or ""] for r in rows])
    return ReportDoc("İhale Takip Listesi", _meta(conn, f, ("Kayıt sayısı", str(len(rows)))), [Section("İhaleler", t)])


def works_list(conn, f: Filters) -> ReportDoc:
    rows = [r for r in PipelineService(conn).works() if f.project_id in (None, r.id)]
    amounts = {c["project_id"]: ContractService(conn).summary(c["id"]).current_amount for c in conn.execute(
        "SELECT id, project_id FROM pp_contracts WHERE kind = 'IDARI'")}
    t = Table([Col("Proje", width=2.4), Col("İdare", width=1.6), Col("İKN", width=1.0), Col("Aşama", width=1.5),
               Col("Kazanma", width=0.9), Col("İdari sözleşme bedeli", M, 1.2)],
              [[r.name, r.authority or "", r.ikn or "", r.stage_label, format_date_tr(r.awarded_date),
                amounts.get(r.id)] for r in rows])
    return ReportDoc("Alınan İşler Listesi", _meta(conn, f, ("Kayıt sayısı", str(len(rows)))), [Section("Alınan işler", t)],
                     ["Sözleşme bedeli: onaylı sözleşme değişiklikleri dahil güncel bedel."])


def project_status(conn, f: Filters) -> ReportDoc:
    pid = f.project_id
    if pid is None:
        raise ValueError("Proje durum özeti için proje seçin.")
    q = lambda sql: conn.execute(sql, (pid,)).fetchone()[0]   # noqa: E731
    IDARI = "SELECT COUNT(*) FROM pp_contracts WHERE kind = 'IDARI' AND project_id = ?"
    TASERON = "SELECT COUNT(*) FROM pp_contracts WHERE kind = 'TASERON' AND project_id = ?"
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    pl = next((r for r in PipelineService(conn).works() + PipelineService(conn).tenders() if r.id == pid), None)
    rep = ActivityPlanService(conn).report(pid)
    items = [
        ["Aşama", (pl.stage_label or pl.tender_label) if pl else p["status"]],
        ["Keşif listesi", str(q("SELECT COUNT(*) FROM estimates WHERE project_id = ?"))],
        ["Sözleşme (idari / taşeron)", f"{q(IDARI)} / {q(TASERON)}"],
        ["Alt yüklenici iş paketi", str(q("SELECT COUNT(*) FROM sub_packages WHERE project_id = ?"))],
        ["Satın alma talebi / sipariş", f"{q('SELECT COUNT(*) FROM purchase_requests WHERE project_id = ?')} / "
                                         f"{q('SELECT COUNT(*) FROM po_orders WHERE project_id = ?')}"],
        ["Hakediş (onaylı / ödenmiş)", str(q("SELECT COUNT(*) FROM pp_payments p JOIN pp_contracts c ON c.id=p.contract_id "
                                              "WHERE c.project_id = ? AND p.status IN ('ONAY','ODENDI')"))],
        ["Faaliyet", str(q("SELECT COUNT(*) FROM ws_activities WHERE project_id = ?"))]]
    money = Table([Col("Gösterge", width=2.5), Col("Tutar", M)],
                  [["Planlanan maliyet (faaliyet planı)", rep.planned_total], ["Gerçekleşen maliyet (KDV hariç)", rep.actual_total],
                   ["Sapma (gerçekleşen − planlanan)", rep.variance], ["Kazanılmış değer", rep.earned_value]])
    return ReportDoc("Proje Durum Özeti", _meta(conn, f),
                     [Section("Kayıt özeti", Table([Col("Başlık", width=2), Col("Değer", width=2)], items)),
                      Section("Maliyet göstergeleri", money)],
                     ["Değerler ilgili modül kayıtlarından okunur; boş modül sıfır olarak görünür."])


# --- Teklifler ve alt yükleniciler --------------------------------------------------------------
def sub_comparison(conn, f: Filters) -> ReportDoc:
    svc = SubcontractService(conn)
    sections, notes = [], []
    for pk in svc.packages(f.project_id):
        if f.discipline and pk["discipline"] != f.discipline:
            continue
        cmp = svc.compare(pk["id"])
        if not cmp.bids:
            continue
        # termin / ödeme / kapsam satırları metin olduğundan sütunlar METİN; tutarlar önceden biçimlenir
        cols = [Col("İş kalemi", width=2.2), Col("Miktar", width=0.8)] + [Col(f"{b.firm} (rev. {b.revision})", width=1.1)
                                                                          for b in cmp.bids]
        rows = [[it.description, f"{format_tr(it.quantity, 3)} {it.unit}"] +
                [format_price_tr(b.unit_prices[it.id]) + (" (kapsam dışı)" if it.id in b.out_of_scope else "")
                 if b.unit_prices.get(it.id) is not None else "—" for b in cmp.bids] for it in cmp.items]
        total = ["Toplam (KDV hariç)", ""] + [format_tr(b.total) + ("" if b.complete else " *") for b in cmp.bids]
        rows += [["Termin (gün)", ""] + [str(b.delivery_days) if b.delivery_days is not None else "" for b in cmp.bids],
                 ["Ödeme koşulu", ""] + [b.payment_terms for b in cmp.bids],
                 ["Kapsam", ""] + ["tam" if b.complete else f"{len(b.missing)} fiyatsız / {len(b.out_of_scope)} kapsam dışı"
                                   for b in cmp.bids]]
        sections.append(Section(f"{pk['name']} — {PACKAGE_STATUSES[pk['status']]}", Table(cols, rows, total)))
        if not cmp.comparable:
            notes.append(f"{pk['name']}: kapsamı eksik teklifler var; toplamlar doğrudan kıyaslanamaz.")
        if pk["decision_note"]:
            notes.append(f"{pk['name']} kararı: {pk['decision_note']} ({pk['decided_by']}).")
    notes.append("En düşük fiyat otomatik olarak en uygun teklif sayılmaz; karar gerekçesi kayıtlıdır.")
    return ReportDoc("Alt Yüklenici Teklif Karşılaştırması", _meta(conn, f), sections or [Section("Teklif yok")], notes)


def firm_bid_history(conn, f: Filters) -> ReportDoc:
    """Firma seçilirse o firmanın, seçilmezse bütün firmaların verdiği teklifler (Firma sütunuyla) listelenir."""
    parties = ([(f.party_id, _name(conn, "parties", f.party_id))] if f.party_id is not None else
               [(r["id"], r["name"]) for r in conn.execute("SELECT id, name FROM parties ORDER BY name")])
    svc, rows, cols = FirmService(conn), [], None
    for pid, pname in parties:
        t = next(x for x in svc.tabs(pid) if x.title == "Verilen teklifler")
        cols = t.columns
        rows += [[pname] + r[0] for r in t.rows if f.in_range(_iso(r[0][3]))
                 and (f.project_id is None or r[0][1] == _name(conn, "projects", f.project_id))]
    table = Table([Col("Firma", ColKind.TEXT, 1.6)] + [Col(c, ColKind.TEXT, 1.0) for c in (cols or [])], rows)
    return ReportDoc("Firma Teklif Geçmişi", _meta(conn, f, ("Kayıt sayısı", str(len(rows)))),
                     [Section("Verilen teklifler", table)],
                     ["Farklı tarih, birim, kapsam ve fiyat koşullarındaki teklifler doğrudan eşdeğer kabul edilmez."])


def purchase_compare_all(conn, f: Filters) -> ReportDoc:
    """Satın alma taleplerindeki bütün firma tekliflerinin yan yana karşılaştırması (talep başına bölümler)."""
    from yapimmaliyet.reports.purchase_reports import purchase_comparison
    svc = PurchaseService(conn)
    sections, notes = [], []
    for req in reversed(svc.list_requests()):
        if f.project_id is not None and req.project_id != f.project_id:
            continue
        if not f.in_range(req.eval_date) and (f.date_from or f.date_to):
            continue
        if not svc.offers(req.id):
            continue
        sel = None
        if req.selected_offer_id:
            row = next((o for o in svc.offers(req.id) if o["id"] == req.selected_offer_id), None)
            sel = f"{row['firm']} (rev. {row['revision']})" if row else None
        doc = purchase_comparison(req, svc.evaluate(req.id), svc.criteria(req.id), svc.scores(req.id), sel)
        for sec in doc.sections:
            sections.append(Section(f"{req.title} — {sec.title}", sec.table, sec.paragraphs))
        notes += [f"{req.title}: {n}" for n in doc.notes]
    return ReportDoc("Satın Alma — Firma Teklifleri Karşılaştırması", _meta(conn, f),
                     sections or [Section("Karşılaştırılacak teklif yok")], notes)


def _iso(text: str) -> str | None:
    try:
        d, m, y = text.split(".")
        return f"{y}-{m}-{d}"
    except ValueError:
        return None


def firm_works(conn, f: Filters) -> ReportDoc:
    if f.party_id is None:
        raise ValueError("Üstlenilen işler için firma seçin.")
    tabs = {t.title: t for t in FirmService(conn).tabs(f.party_id)}
    t = tabs["Alınan işler"]
    return ReportDoc("Firma — Üstlenilen İşler", _meta(conn, f),
                     [Section("Alınan işler", Table([Col(c) for c in t.columns], [r[0] for r in t.rows])),
                      Section("Disiplinler ve iş kalemleri", Table([Col(c) for c in tabs["Disiplinler ve iş kalemleri"].columns],
                                                                   [r[0] for r in tabs["Disiplinler ve iş kalemleri"].rows]))])


# --- Sözleşmeler -------------------------------------------------------------------------------
def contract_summary_report(conn, f: Filters) -> ReportDoc:
    svc = ContractService(conn)
    rows = []
    for c in conn.execute("SELECT id FROM pp_contracts WHERE (? IS NULL OR project_id = ?) ORDER BY id", (f.project_id, f.project_id)):
        s = svc.summary(c["id"])
        k = s.contract
        rows.append([k.title, "Taşeron" if k.kind == "TASERON" else "İdari", k.employer, k.contractor, k.contract_no or "",
                     format_date_tr(k.contract_date), k.contract_amount, s.increase, s.decrease, s.current_amount,
                     format_date_tr(s.current_end), len(s.pending)])
    t = Table([Col("Sözleşme", width=2), Col("Tür", width=0.7), Col("İşveren", width=1.4), Col("Yüklenici", width=1.4),
               Col("No", width=0.8), Col("Tarih", width=0.8), Col("Asıl bedel", M), Col("Artış", M), Col("Eksiliş", M),
               Col("Güncel bedel", M), Col("Güncel bitiş", width=0.9), Col("Onay bekleyen", I, 0.8)], rows)
    return ReportDoc("Sözleşme Özeti", _meta(conn, f), [Section("Sözleşmeler", t)],
                     ["Güncel bedel ve bitiş yalnız onaylı değişiklikleri içerir; onay bekleyenler ayrıca sayılır."])


def contract_changes(conn, f: Filters) -> ReportDoc:
    rows = []
    for c in conn.execute("SELECT id, title FROM pp_contracts WHERE (? IS NULL OR project_id = ?) ORDER BY id",
                          (f.project_id, f.project_id)):
        for ch in ContractService(conn).changes(c["id"]):
            if not f.in_range(ch.approval_date or ch.created_at[:10]):
                continue
            rows.append([c["title"], ch.change_no, ch.kind, ch.title, "Onaylı" if ch.approved else "Onay bekliyor",
                         format_date_tr(ch.approval_date), ch.approval_ref or "", ch.amount_delta, ch.days_delta])
    t = Table([Col("Sözleşme", width=1.8), Col("No", I, 0.4), Col("Tür", width=1.0), Col("Konu", width=2),
               Col("Durum", width=0.9), Col("Onay tarihi", width=0.9), Col("Onay sayısı", width=0.9),
               Col("Tutar farkı", M), Col("Gün farkı", I, 0.6)], rows)
    inc = sum((r[7] for r in rows if r[7] and r[7] > 0 and r[4] == "Onaylı"), ZERO)
    dec = sum((r[7] for r in rows if r[7] and r[7] < 0 and r[4] == "Onaylı"), ZERO)
    return ReportDoc("Sözleşme Değişiklikleri — İş Artışı / Eksilişi ve Süre", _meta(conn, f),
                     [Section("Değişiklikler", t, [f"Onaylı iş artışı toplamı: {inc}; onaylı iş eksilişi toplamı: {dec}"])],
                     ["Onay bekleyen değişiklikler güncel bedel ve süreye yansımaz."])


# --- Satın alma ----------------------------------------------------------------------------------
def order_status(conn, f: Filters) -> ReportDoc:
    rows = []
    for o in OrderService(conn).orders(f.project_id):
        if not f.in_range(o.order_date) or (f.party_id and not conn.execute(
                "SELECT 1 FROM purchase_offers WHERE id = ? AND party_id = ?", (o.offer_id, f.party_id)).fetchone()):
            continue
        rows.append([o.order_no, format_date_tr(o.order_date), o.supplier, format_date_tr(o.due_date),
                     o.status + (" · gecikmede" if o.late() else ""), o.total_excl_vat, o.delivered_amount,
                     sum((ln.remaining for ln in o.lines), ZERO)])
    t = Table([Col("Sipariş no"), Col("Tarih"), Col("Tedarikçi", width=2), Col("Termin"), Col("Durum", width=1.3),
               Col("Sipariş (KDV hariç)", M), Col("Teslim edilen", M), Col("Kalan miktar toplamı", Q)], rows)
    return ReportDoc("Sipariş ve Teslim Durum Raporu", _meta(conn, f), [Section("Siparişler", t)],
                     ["Kalan miktar farklı birimlerdeki kalemlerin toplamıdır; yalnız bekleyen teslim olup olmadığını gösterir."])


# --- Finans --------------------------------------------------------------------------------------
def cari_statement(conn, f: Filters) -> ReportDoc:
    if f.party_id is None:
        raise ValueError("Cari ekstre için firma seçin.")
    svc = CariService(conn)
    st = [e for e in svc.statement(f.party_id) if f.in_range(e.entry_date)]
    t = Table([Col("Tarih"), Col("Tür", width=1.4), Col("Belge no"), Col("Açıklama", width=2.4), Col("Vade"),
               Col("Borç", M), Col("Alacak", M), Col("Bakiye", M)],
              [[format_date_tr(e.entry_date), CARI_KINDS[e.kind], e.document_no, e.description, format_date_tr(e.due_date),
                e.debit or None, e.credit or None, e.balance] for e in st])
    op = Table([Col("Vade"), Col("Belge / açıklama", width=2.5), Col("Kalan tutar", M), Col("Durum")],
               [[format_date_tr(i.due), i.entry.document_no or i.entry.description, i.remaining,
                 "Gecikmiş" if i.due < date.today().isoformat() else "Açık"] for i in svc.open_items(f.party_id)])
    return ReportDoc("Cari Hesap Ekstresi", _meta(conn, f), [Section("Hareketler", t), Section("Açık kalemler", op)],
                     ["Bakiye (B): cari borçlu; negatif: cari alacaklı. Açık kalemler en eski vade önce kapanacak şekilde eşleştirilir."])


def finance_summary(conn, f: Filters) -> ReportDoc:
    tr, notes_svc, debt = TreasuryService(conn), NoteService(conn), DebtService(conn)
    acc = Table([Col("Hesap", width=2), Col("Tür"), Col("Bakiye", M)],
                [[a.name, "Banka" if a.kind == "BANKA" else "Kasa", a.balance] for a in tr.accounts()],
                ["Toplam nakit", "", tr.total_cash()])
    nts = Table([Col("Vade"), Col("Yön"), Col("Cari", width=2), Col("No"), Col("Durum"), Col("Tutar", M)],
                [[format_date_tr(n.due_date), DIRECTIONS[n.direction], n.party_name, n.number, NOTE_STATUSES[n.status], n.amount]
                 for n in notes_svc.notes(open_only=True) if f.in_range(n.due_date)])
    dbt = Table([Col("Borç", width=2), Col("Tür"), Col("Kalan", M), Col("Yıllık faiz %", M), Col("Aylık ödeme", M)],
                [[d.name, d.kind_label, d.balance, d.annual_rate, d.monthly_payment] for d in debt.debts(False)],
                ["Toplam", "", debt.summary()["total"], None, debt.summary()["load"]])
    cr = CariService(conn).totals()
    cari = Table([Col("Gösterge", width=2.5), Col("Tutar", M)], [["Toplam cari alacak", cr["receivable"]],
                 ["Toplam cari borç", cr["payable"]], ["Vadesi geçen alacak", cr["overdue_receivable"]],
                 ["Vadesi geçen borç", cr["overdue_payable"]]])
    return ReportDoc("Finans Özeti — Kasa/Banka, Cari, Çek-Senet, Borç", _meta(conn, f),
                     [Section("Kasa ve banka", acc), Section("Cari", cari), Section("Portföydeki çek / senetler", nts),
                      Section("Açık krediler ve borçlar", dbt)])


# --- Maliyet ve iş programı -----------------------------------------------------------------------
def plan_vs_actual(conn, f: Filters) -> ReportDoc:
    if f.project_id is None:
        raise ValueError("Planlanan / gerçekleşen maliyet için proje seçin.")
    rep = ActivityPlanService(conn).report(f.project_id, f.discipline)
    rows = [r for r in rep.rows if f.in_range(r.period + "-01") or (not f.date_from and not f.date_to)]
    t = Table([Col("Dönem"), Col("Planlanan", M), Col("Gerçekleşen", M), Col("Sapma", M), Col("Küm. planlanan", M),
               Col("Küm. gerçekleşen", M)],
              [[r.period, r.planned, None if f.discipline else r.actual, None if f.discipline else r.variance,
                r.cum_planned, None if f.discipline else r.cum_actual] for r in rows],
              ["Toplam", rep.planned_total, None if f.discipline else rep.actual_total,
               None if f.discipline else rep.variance, None, None])
    ev = Table([Col("Gösterge", width=2.5), Col("Değer", width=1)],
               [["Kazanılmış değer (planlanan maliyet × ilerleme)", format_tr(rep.earned_value)],
                ["Planlanan maliyet ağırlıklı ilerleme %", format_tr(rep.progress_weighted) if rep.progress_weighted is not None else ""],
                ["Maliyet performansı (CPI)", format_tr(rep.cpi) if rep.cpi is not None else "hesaplanamadı"]])
    notes = ["Planlanan maliyet faaliyet tarihleri arasına gün oranıyla dağıtılır; gerçekleşen maliyet gider kayıtları, "
             "satın alma teslimleri ve taşeron hakedişlerinden (KDV hariç) okunur.",
             "Maliyet, hakediş geliri ve nakit çıkışı birbirine eşit değildir."]
    if f.discipline:
        notes.append("Gerçekleşen maliyet disiplin bazında izlenmediğinden süzgeçte gösterilmez.")
    return ReportDoc("Planlanan / Gerçekleşen Maliyet ve Bütçe Sapması", _meta(conn, f),
                     [Section("Dönemsel karşılaştırma (KDV hariç)", t), Section("İlerleme ve maliyet", ev)], notes)


def activity_progress(conn, f: Filters) -> ReportDoc:
    if f.project_id is None:
        raise ValueError("Faaliyet ilerleme raporu için proje seçin.")
    costs, unassigned = ActivityPlanService(conn).activity_costs(f.project_id)
    acts = [c for c in costs if (not f.discipline or c.activity.discipline == f.discipline)]
    t = Table([Col("Faaliyet", width=2.2), Col("Disiplin"), Col("Başlangıç"), Col("Bitiş"), Col("Planlanan", M),
               Col("İlerleme %", PC, 0.7), Col("Kazanılmış", M), Col("Gerçekleşen", M), Col("Sapma", M), Col("CPI", width=0.6)],
              [[c.activity.name, DISCIPLINES.get(c.activity.discipline or "", ""), format_date_tr(c.activity.planned_start),
                format_date_tr(c.activity.planned_end), c.activity.planned_cost, c.activity.progress / 100,
                c.activity.earned, c.actual, c.variance, format_tr(c.cpi) if c.cpi is not None else ""] for c in acts],
              ["Toplam", "", "", "", sum((c.activity.planned_cost for c in acts), ZERO), None,
               sum((c.activity.earned for c in acts), ZERO), sum((c.actual for c in acts), ZERO), None, ""])
    return ReportDoc("Faaliyet İlerleme Raporu", _meta(conn, f), [Section("Faaliyetler", t)],
                     ["Faaliyet gerçekleşeni: ona atanmış gider kayıtları + bağlı iş paketinin taşeron hakedişleri (KDV hariç).",
                      f"Faaliyete atanmamış maliyet (teslimler, atanmamış giderler): {format_tr(unassigned)}"])


CATALOG: list[ReportDef] = [
    ReportDef("IHALE_TAKIP", "İhale takip listesi", "Projeler ve İhaleler", ("project", "dates"), tender_list),
    ReportDef("ALINAN_ISLER", "Alınan işler listesi", "Projeler ve İhaleler", ("project",), works_list),
    ReportDef("PROJE_DURUM", "Proje durum özeti", "Projeler ve İhaleler", ("project",), project_status),
    ReportDef("ALT_YUK_KARS", "Alt yüklenici teklif karşılaştırması", "Teklifler ve Alt Yükleniciler",
              ("project", "discipline"), sub_comparison),
    ReportDef("FIRMA_TEKLIF", "Firma teklif geçmişi (firma seçilmezse tüm firmalar)", "Teklifler ve Alt Yükleniciler",
              ("party", "project", "dates"), firm_bid_history),
    ReportDef("SATINALMA_KARS", "Satın alma — firma teklifleri karşılaştırması", "Teklifler ve Alt Yükleniciler",
              ("project", "dates"), purchase_compare_all),
    ReportDef("FIRMA_ISLER", "Üstlenilen işler listesi", "Teklifler ve Alt Yükleniciler", ("party",), firm_works),
    ReportDef("SOZLESME_OZET", "Sözleşme özeti", "Sözleşmeler", ("project",), contract_summary_report),
    ReportDef("SOZLESME_DEGISIKLIK", "Sözleşme değişiklikleri (iş artışı/eksilişi, süre)", "Sözleşmeler",
              ("project", "dates"), contract_changes),
    ReportDef("SIPARIS_DURUM", "Sipariş ve teslim durum raporu", "Satın Alma", ("project", "dates", "party"), order_status),
    ReportDef("CARI_EKSTRE", "Cari hesap ekstresi", "Finans", ("party", "dates"), cari_statement),
    ReportDef("FINANS_OZET", "Finans özeti (kasa, cari, çek/senet, borç)", "Finans", ("dates",), finance_summary),
    ReportDef("PLAN_GERCEKLESEN", "Planlanan / gerçekleşen maliyet ve sapma", "Maliyet ve İş Programı",
              ("project", "dates", "discipline"), plan_vs_actual),
    ReportDef("FAALIYET_ILERLEME", "Faaliyet ilerleme raporu", "Maliyet ve İş Programı", ("project", "discipline"),
              activity_progress),
]
from yapimmaliyet.reports.module_reports2 import EXTRA  # noqa: E402  (aynı kataloğun ikinci bölümü)

from yapimmaliyet.reports.module_reports3 import EXTRA3  # noqa: E402  (proje kontrol raporları)

CATALOG += EXTRA + EXTRA3
BY_KEY = {r.key: r for r in CATALOG}


def build(conn: sqlite3.Connection, key: str, f: Filters) -> ReportDoc:
    return BY_KEY[key].build(conn, f)
