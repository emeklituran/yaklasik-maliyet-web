"""Proje kontrolü raporları: iş programı ve ilerleme, kesin hesap, satın alma siparişi / teslim, gerçekleşen maliyet ve
nakit akışı. Değerler servislerden gelir; burada yalnız rapor modeli kurulur."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
from yapimmaliyet.services.cost_tracking_service import CATEGORIES, CashFlow, CostReport
from yapimmaliyet.services.final_account_service import FinalAccount
from yapimmaliyet.services.order_service import Order
from yapimmaliyet.services.schedule_service import Schedule, period_label

M, Q, P, PC = ColKind.MONEY, ColKind.QTY, ColKind.PRICE, ColKind.PERCENT


def _meta(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs) + [("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")), ("Uygulama sürümü", __version__)]


def schedule_report(s: Schedule) -> ReportDoc:
    c = s.summary.contract
    t = Table([Col("Dönem", width=0.8), Col("Planlanan", M), Col("Planlanan küm.", M), Col("Plan %", PC, 0.7),
               Col("Gerçekleşen", M), Col("Gerçekleşen küm.", M), Col("Gerçekleşen %", PC, 0.8), Col("Sapma", M),
               Col("Not", width=1.4)],
              [[period_label(r.period), r.planned, r.planned_cum, r.planned_pct, r.actual, r.actual_cum, r.actual_pct,
                r.deviation, r.note or ""] for r in s.rows],
              ["Toplam", s.plan_total, None, None, s.actual_total, None, None, None, ""])
    return ReportDoc("İş Programı ve İlerleme", _meta(
        ("İşin adı", c.title), ("İşveren", c.employer), ("Yüklenici", c.contractor),
        ("Güncel sözleşme bedeli", f"{s.current_amount} {c.currency}"),
        ("Başlangıç / güncel bitiş", f"{format_date_tr(c.start_date)} / {format_date_tr(s.summary.current_end)}")),
        [Section("Aylık plan ve gerçekleşme (KDV ve fiyat farkı hariç)", t)],
        s.warnings + ["Gerçekleşen: onaylı / ödendi hakedişlerin dönem iş tutarı, dönem sonunun ayında."])


def final_account_report(fa: FinalAccount) -> ReportDoc:
    c = fa.summary.contract
    rec = fa.record or {}
    lines = Table([Col("Sıra", ColKind.INT, 0.4), Col("Poz no", width=0.9), Col("Tanım", width=2.6),
                   Col("Birim", width=0.5), Col("Sözleşme miktarı", Q, 0.9), Col("Birim fiyat", P, 0.9),
                   Col("Sözleşme tutarı", M), Col("Kesin miktar", Q, 0.9), Col("Kesin tutar", M), Col("Fark", M)],
                  [[ln.line_no, ln.code or "", ln.description, ln.unit or "", ln.contract_qty, ln.unit_price,
                    ln.contract_amount, ln.final_qty, ln.final_amount, ln.difference] for ln in fa.lines],
                  [None, "", "Toplam", "", None, None, sum((ln.contract_amount for ln in fa.lines), Decimal(0)), None,
                   fa.work_total, fa.work_total - sum((ln.contract_amount for ln in fa.lines), Decimal(0))])
    amounts = fa.snapshot_amounts or {"contract_amount": c.contract_amount, "current_amount": fa.current_amount}
    summary = Table([Col("Kalem", width=2.5), Col("Tutar", M)], [
        ["Asıl sözleşme bedeli", amounts["contract_amount"]],
        ["Değişikliklerle güncel sözleşme bedeli", amounts["current_amount"]],
        ["Kesin iş tutarı (fiyat farkı hariç)", fa.work_total], ["Güncel bedele göre fark", fa.difference],
        ["Toplam fiyat farkı", fa.price_diff_total], ["Toplam KDV", fa.vat_total],
        ["Onaylı hakedişlerin ödenecek toplamı", fa.payable_total], ["Ödendi işaretli toplam", fa.paid_total],
        ["Ödenmemiş (onaylı) hakediş", fa.unpaid], ["Avans", fa.advance_total],
        ["Mahsup edilen avans", fa.advance_recovered],
        ["İade edilecek teminat kesintileri (kesin kabulde)", fa.guarantee_total]])
    ded = Table([Col("Kesinti", width=2.5), Col("Toplam", M)], [[k, v] for k, v in fa.deductions.items()])
    pays = Table([Col("No", ColKind.INT, 0.4), Col("Dönem sonu", width=0.9), Col("Bu dönem iş", M),
                  Col("Fiyat farkı", M), Col("KDV", M), Col("Kesintiler", M), Col("Ödenecek", M)],
                 [[p["no"], format_date_tr(p["period_end"]), Decimal(p["work_period"]), Decimal(p["price_diff"] or 0),
                   Decimal(p["vat"]), Decimal(p["deductions_total"]), Decimal(p["payable"])] for p in fa.payments])
    status = "KESİNLEŞTİ" if fa.final else "TASLAK (kesinleşmedi)"
    doc = ReportDoc("Kesin Hesap", _meta(
        ("İşin adı", c.title), ("İşveren", c.employer), ("Yüklenici", c.contractor),
        ("Sözleşme no / tarih", f"{c.contract_no or '—'} / {format_date_tr(c.contract_date)}"),
        ("İşin bitim (geçici kabul) tarihi", format_date_tr(rec.get("completion_date"))),
        ("Geçici kabul tutanağı", rec.get("acceptance_ref") or "—"), ("Durum", status),
        ("Kesinleştiren", f"{rec.get('approved_by') or '—'} {rec.get('approved_at') or ''}".strip())),
        [Section("Kalem bazında kesin miktar ve tutarlar", lines), Section("Kesin hesap özeti (TL)", summary),
         Section("Kesintiler toplamı", ded), Section("Hakedişler", pays),
         Section("İmzalar", paragraphs=["Düzenleyen: ……………………  İmza: ……………",
                                        "Kontrol eden: ……………………  İmza: ……………",
                                        "Onaylayan: ……………………  İmza: ……………  Tarih: ……………"])],
        fa.warnings + ["Kalemler ve miktarlar son geçerli hakedişin kesinleşmiş kaydından alınmıştır."])
    doc.draft = not fa.final
    return doc


def order_report(o: Order) -> ReportDoc:
    lines = Table([Col("Sıra", ColKind.INT, 0.4), Col("Kalem", width=2.4), Col("Birim", width=0.5),
                   Col("Sipariş miktarı", Q), Col("Net birim fiyat", P), Col("Tutar", M), Col("Teslim edilen", Q),
                   Col("Kalan", Q)],
                  [[ln.line_no, ln.description, ln.unit, ln.quantity, ln.unit_price, ln.amount, ln.delivered,
                    ln.remaining] for ln in o.lines],
                  [None, "Mal toplamı", "", None, None, o.goods_total, None, None])
    totals = Table([Col("Kalem", width=2), Col("Tutar", M)], [
        ["Mal toplamı", o.goods_total], ["Ek giderler (nakliye, montaj, diğer)", o.extra_costs],
        ["KDV hariç toplam", o.total_excl_vat], [f"KDV (%{o.vat_rate})", o.vat],
        ["KDV dahil toplam", o.total_excl_vat + o.vat], ["Teslim edilen (KDV hariç)", o.delivered_amount]])
    dels = Table([Col("Teslim tarihi", width=0.9), Col("İrsaliye", width=0.9), Col("Teslim alan", width=1),
                  Col("Mal tutarı", M), Col("Ek gider payı", M), Col("KDV", M), Col("Ödeme tarihi", width=0.9)],
                 [[format_date_tr(d.delivery_date), d.waybill_no or "", d.received_by or "", d.goods, d.extra, d.vat,
                   format_date_tr(d.paid_date)] for d in o.deliveries])
    return ReportDoc("Satın Alma Siparişi ve Teslim Durumu", _meta(
        ("Sipariş no", o.order_no), ("Sipariş tarihi", format_date_tr(o.order_date)), ("Tedarikçi", o.supplier),
        ("Termin", format_date_tr(o.due_date)), ("Durum", o.status), ("Satın alma talebi", f"#{o.request_id}")),
        [Section("Sipariş kalemleri", lines), Section("Tutarlar (TL)", totals), Section("Teslimler", dels)],
        ["Ek giderler teslim edilen mal tutarı oranında teslimlere dağıtılır."] + ([o.notes] if o.notes else []))


def cost_report(project_name: str, rep: CostReport, cf: CashFlow) -> ReportDoc:
    budget = Table([Col("Gider türü", width=1.4), Col("Bütçe", M), Col("Gerçekleşen", M), Col("Fark (bütçe − gerç.)", M),
                    Col("Gerçekleşme %", PC, 0.8)],
                   [[CATEGORIES[r.category], r.budget, r.actual, r.variance, r.ratio] for r in rep.rows],
                   ["Toplam", rep.budget_total, rep.actual_total, rep.budget_total - rep.actual_total, None])
    profit = Table([Col("Kalem", width=2.4), Col("Tutar", M)], [
        ["İdari sözleşmelerin güncel bedeli", rep.contract_amount],
        ["Kazanılmış hakediş geliri (iş + fiyat farkı)", rep.earned], ["Gerçekleşen maliyet", rep.actual_total],
        ["Fark (gelir − maliyet)", rep.margin]])
    lines = Table([Col("Tarih", width=0.8), Col("Tür", width=1), Col("Kaynak", width=1.1), Col("Açıklama", width=2.6),
                   Col("Tutar (KDV hariç)", M), Col("Ödeme tutarı", M), Col("Ödeme tarihi", width=0.8)],
                  [[format_date_tr(ln.date), CATEGORIES[ln.category], ln.source, ln.description, ln.amount, ln.cash,
                    format_date_tr(ln.paid_date)] for ln in rep.lines])
    cash = Table([Col("Dönem", width=0.8), Col("Giriş", M), Col("Çıkış", M), Col("Net", M), Col("Kümülatif", M),
                  Col("Programdaki iş (bilgi)", M)],
                 [[period_label(r.period), r.inflow, r.outflow, r.net, r.cumulative, r.planned_work] for r in cf.rows],
                 ["Toplam", cf.inflow_total, cf.outflow_total, cf.inflow_total - cf.outflow_total, None, None])
    return ReportDoc("Gerçekleşen Maliyet ve Nakit Akışı", _meta(("Proje", project_name)), [
        Section("Bütçe ve gerçekleşen maliyet (KDV hariç)", budget), Section("Gelir ve maliyet", profit),
        Section("Nakit akışı (KDV dahil, ödeme tarihine göre)", cash), Section("Gerçekleşen maliyet kayıtları", lines)],
        cf.notes + ["Satın alma teslimleri ve taşeron hakedişleri kendi kayıtlarından okunur; kopyalanmaz."])
