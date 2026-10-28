"""Proje kontrol raporları (şartname §14): faaliyet bazlı plan/gerçekleşme (PV/EV/AC/CPI/SPI), faaliyet–keşif eşleme, maliyet kaynağı
mutabakatı, taahhüt durumu, EAC ve kârlılık, plan revizyon karşılaştırması, veri kalite raporu.

Her rapor tek servis sonucundan üretilir (PDF/XLSX/ekran aynı sayıları taşır). Üstbilgide proje, rapor tarihi, plan sürümü,
hesaplama yöntemi ve veri kesim tarihi bulunur."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from yapimmaliyet.domain.money import format_tr
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
from yapimmaliyet.reports.module_reports import Filters, ReportDef, _meta
from yapimmaliyet.services.commitment_service import CommitmentService
from yapimmaliyet.services.evm_service import EvmService
from yapimmaliyet.services.forecast_service import SCENARIOS, ForecastService
from yapimmaliyet.services.schedule_control_service import SOURCE_TYPES, ScheduleControlService

M, Q, P, PC, I = ColKind.MONEY, ColKind.QTY, ColKind.PRICE, ColKind.PERCENT, ColKind.INT
ZERO = Decimal(0)


def _project(f: Filters, what: str) -> int:
    if f.project_id is None:
        raise ValueError(f"{what} için proje seçin.")
    return f.project_id


def _as_of(f: Filters) -> str:
    return f.date_to or date.today().isoformat()


def _na(v: Decimal | None) -> str:
    return "N/A" if v is None else format_tr(v, 2)


def _meta_ctl(conn, f: Filters, as_of: str, plan_label: str, method: str) -> list[tuple[str, str]]:
    return _meta(conn, f, ("Veri kesim tarihi", format_date_tr(as_of)), ("Plan sürümü", plan_label), ("Hesaplama yöntemi", method),
                 ("Tutarlar", "TL, KDV hariç"))


def evm_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Faaliyet bazlı plan/gerçekleşme raporu")
    as_of = _as_of(f)
    r = EvmService(conn).evaluate(pid, as_of)
    rows = [x for x in r.rows if not f.discipline or _disc(conn, x.activity_id) == f.discipline]
    t = Table([Col("Kod", width=0.7), Col("Faaliyet", width=2.2), Col("BAC", M), Col("PV", M), Col("EV", M), Col("AC", M),
               Col("İlerleme %", ColKind.TEXT, 0.7), Col("CPI", ColKind.TEXT, 0.5), Col("SPI", ColKind.TEXT, 0.5)],
              [[x.code, x.title, x.bac, x.pv, x.ev, x.ac, "—" if x.progress is None else format_tr(x.progress, 0), _na(x.cpi), _na(x.spi)]
               for x in rows],
              ["Toplam", "", sum((x.bac for x in rows), ZERO), sum((x.pv for x in rows), ZERO), sum((x.ev for x in rows), ZERO),
               sum((x.ac for x in rows), ZERO), "", "", ""])
    kpi = Table([Col("Gösterge", width=2), Col("Değer", ColKind.TEXT, 1.5)],
                [["BAC (" + r.bac_source + ")", _na(r.bac)], ["PV", format_tr(r.pv)], ["EV", format_tr(r.ev)],
                 ["AC (proje toplamı)", format_tr(r.ac)], ["  — faaliyete tahsis edilmemiş AC", format_tr(r.unallocated_ac)],
                 ["CV = EV − AC", format_tr(r.cv)], ["SV = EV − PV", format_tr(r.sv)], ["CPI = EV / AC", _na(r.cpi)],
                 ["SPI = EV / PV", _na(r.spi)]])
    plan = f"Baz plan rev.{r.baseline.revision_no}" if r.baseline else "Güncel plan (baz plan yok)"
    return ReportDoc("Faaliyet Bazlı Plan / Gerçekleşme (PV, EV, AC, CPI, SPI)", _meta_ctl(conn, f, as_of, plan, r.method),
                     [Section("Proje göstergeleri", kpi), Section("Faaliyetler", t)],
                     r.warnings + ["Payda sıfırsa gösterge N/A'dır. PV takvim günü esaslıdır; sahadaki ilerlemeyi kanıtlamaz."])


def _disc(conn, activity_id):
    r = conn.execute("SELECT discipline FROM ws_activities WHERE id = ?", (activity_id,)).fetchone() if activity_id else None
    return r["discipline"] if r else None


def scope_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Faaliyet–keşif eşleme raporu")
    rows = ScheduleControlService(conn).scope_report(pid)
    t = Table([Col("Sıra", I, 0.4), Col("Poz", width=0.8), Col("Keşif kalemi", width=2.4), Col("Birim", width=0.5), Col("Miktar", Q, 0.8),
               Col("Tahsisli %", ColKind.TEXT, 0.7), Col("Kalan %", ColKind.TEXT, 0.7), Col("Faaliyetler", width=2.2), Col("Uyarı", width=0.9)],
              [[r["line_no"], r["code"], r["description"], r["unit"], r["quantity"], format_tr(r["allocated_pct"], 0),
                format_tr(r["unallocated_pct"], 0), "; ".join(f"{n} (%{format_tr(p, 0)})" for n, p in r["activities"]), r["warning"]]
               for r in rows])
    n0 = len([r for r in rows if r["allocated_pct"] == 0])
    return ReportDoc("Faaliyet – Keşif Eşleme", _meta_ctl(conn, f, _as_of(f), "Güncel plan", "Oranlı kapsam tahsisi (Σ ≤ %100)"),
                     [Section("Keşif kalemleri ve faaliyet tahsisleri", t)],
                     [f"{n0} keşif kalemi hiçbir faaliyete bağlı değil." if n0 else "Tüm keşif kalemleri en az bir faaliyete bağlı.",
                      "Bir kalem birden çok faaliyete, bir faaliyet birden çok kaleme bağlanabilir; kalem başına toplam tahsis %100'ü aşamaz."])


def reconciliation_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Maliyet kaynağı mutabakatı")
    as_of = _as_of(f)
    ctl = ScheduleControlService(conn)
    names = {a.id: a.name for a in ctl.plan.activities(pid)}
    rec = ctl.reconcile(pid, as_of)
    rows = []
    for s in ctl.cost_sources(pid, as_of):
        dist = "; ".join(f"{names.get(a.activity_id, '?')} %{format_tr(a.ratio, 2)}={format_tr(a.amount)}" + (" (paket bağı)" if a.implicit else "")
                         for a in s.allocations)
        rows.append([SOURCE_TYPES[s.source_type], s.description, format_date_tr(s.date), s.amount, dist, s.allocated, s.unallocated])
    t = Table([Col("Kaynak türü", width=1.1), Col("Kaynak belge", width=2.3), Col("Tarih", width=0.8), Col("Kaynak tutar", M),
               Col("Faaliyet tahsisleri", width=2.6), Col("Tahsisli", M), Col("Bakiye", M)], rows,
              ["Toplam", "", "", rec["source_total"], "", rec["allocated"], rec["unallocated"]])
    return ReportDoc("Maliyet Kaynağı Mutabakatı", _meta_ctl(conn, f, as_of, "—", "Kaynak tutar tek; tahsis oranlıdır (kopya yok)"),
                     [Section("Kaynak maliyet olayları ve faaliyet tahsisleri", t)],
                     [f"Tahsis edilmemiş toplam: {format_tr(rec['unallocated'])} — proje maliyetine dahildir, faaliyete atanmamıştır.",
                      "Kaynak toplamı tahsislerden etkilenmez: Σ tahsisli + bakiye = Σ kaynak."])


def commitment_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Taahhüt durum raporu")
    as_of = _as_of(f)
    s = CommitmentService(conn).summary(pid, as_of)
    t = Table([Col("Tür", width=0.8), Col("Taahhüt", width=2.2), Col("Firma", width=1.5), Col("Taahhüt tutarı", M), Col("Gerçekleşen", M),
               Col("Açık taahhüt", M), Col("Ödenen (KDV dahil nakit)", M), Col("Not", width=2)],
              [["Sipariş" if c.kind == "SIPARIS" else "Taşeron", c.ref, c.party, c.committed, c.realized, c.open, c.paid,
                "; ".join(filter(None, [c.note, *c.flags]))] for c in s.rows],
              ["Toplam", "", "", s.committed, s.realized, s.open, s.paid, ""])
    return ReportDoc("Taahhüt Durum Raporu", _meta_ctl(conn, f, as_of, "—", "Taahhüt − gerçekleşen = açık; ödeme nakittir, maliyet değildir"),
                     [Section("Sipariş ve taşeron taahhütleri", t)],
                     [f"Onay bekleyen sözleşme değişikliği {format_tr(s.pending_changes)}: taahhüde dahil değil." if s.pending_changes else
                      "Onay bekleyen sözleşme değişikliği yok.",
                      "Fazla gerçekleşme (açık < 0) istisnadır; toplam açık taahhütte 0 alınır."])


def eac_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "EAC ve kârlılık raporu")
    as_of = _as_of(f)
    svc = ForecastService(conn)
    fs = svc.scenarios(pid, as_of)
    t = Table([Col("Senaryo", width=1)] + [Col(c, M, 1) for c in ("AC", "Açık taahhüt", "Taahhütsüz ETC", "EAC", "Gelir", "Beklenen kâr")] +
              [Col("Marj %", ColKind.TEXT, 0.6), Col("Durum", width=1.1)],
              [[SCENARIOS[x.scenario], x.ac, x.open_commitment, x.uncommitted_etc if x.uncommitted_etc is not None else None, x.eac,
                x.revenue, x.profit, _na(x.margin), "Tam" if x.complete else "Eksik (ETC girilmemiş)"] for x in fs])
    base = next(x for x in fs if x.scenario == "BAZ")
    trace = Table([Col("Hesap dökümü (baz senaryo)", width=1)], [[x] for x in base.trace])
    return ReportDoc("EAC ve Kârlılık", _meta_ctl(conn, f, as_of, "—", "EAC = AC + açık taahhüt + taahhütsüz ETC; kâr = onaylı gelir − EAC"),
                     [Section("Senaryolar", t), Section("Kaynak izi", trace)], base.warnings +
                     ["Tahmin kullanıcı onaylı varsayımdır (sürüm, gerekçe, tarih kayıtlıdır); gerçek sonuç değildir. Onaysız değişiklikler gelire/taahhüde girmez."])


def revision_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Plan revizyon karşılaştırması")
    ctl = ScheduleControlService(conn)
    locked = [b for b in ctl.baselines(pid) if b.locked]
    if len(locked) < 2:
        raise ValueError("Karşılaştırma için en az iki kilitli baz plan sürümü gerekir.")
    a, b = locked[0], locked[-1]
    rows = [[d["title"], d["durum"], format_date_tr(d["end_a"]), format_date_tr(d["end_b"]), d["days_shift"], d["cost_a"], d["cost_b"], d["cost_diff"]]
            for d in ctl.compare(a.id, b.id)]
    t = Table([Col("Faaliyet", width=2.4), Col("Durum", width=0.8), Col("Bitiş (ilk)", width=0.9), Col("Bitiş (son)", width=0.9),
               Col("Gün kayması", I, 0.7), Col("Maliyet (ilk)", M), Col("Maliyet (son)", M), Col("Fark", M)], rows,
              ["Toplam", "", "", "", None, sum((r[5] for r in rows), ZERO), sum((r[6] for r in rows), ZERO), sum((r[7] for r in rows), ZERO)])
    return ReportDoc("Plan Revizyon Karşılaştırması", _meta_ctl(conn, f, _as_of(f), f"Rev.{a.revision_no} → Rev.{b.revision_no}", "Kilitli baz plan anlık görüntüleri"),
                     [Section(f"Baz plan rev.{a.revision_no} ({a.name}) ↔ rev.{b.revision_no} ({b.name})", t)],
                     [f"Son revizyon gerekçesi: {b.reason or '—'} ({b.approved_by}, {format_date_tr((b.approved_at or '')[:10])})"])


def quality_report(conn, f: Filters) -> ReportDoc:
    pid = _project(f, "Veri kalite raporu")
    as_of = _as_of(f)
    evm = EvmService(conn).evaluate(pid, as_of)
    fc = ForecastService(conn).evaluate(pid, as_of)
    ctl = ScheduleControlService(conn)
    rows = [["Plan", w] for w in evm.warnings] + [["Tahmin", w] for w in fc.warnings if w not in evm.warnings]
    for s in ctl.reconcile(pid, as_of)["unallocated_sources"][:50]:
        rows.append(["Tahsis", f"{SOURCE_TYPES[s.source_type]}: {s.description} — tahsis edilmemiş {format_tr(s.unallocated)}"])
    rows += [["Taahhüt", f"{c.ref}: {'; '.join(c.flags)}"] for c in fc.commitments.exceptions]
    t = Table([Col("Alan", width=0.8), Col("Bulgu", width=5)], rows or [["—", "Bulgu yok."]])
    return ReportDoc("Veri Kalite Raporu", _meta_ctl(conn, f, as_of, "—", "Otomatik tutarlılık denetimi"), [Section("Bulgular", t)],
                     ["Eşleşmeyen faaliyet/keşif, tahsis edilmemiş maliyet, eksik ilerleme, eksik ETC ve taahhüt istisnaları listelenir."])


def performance_report(conn, f: Filters) -> ReportDoc:
    from yapimmaliyet.services.performance_service import PerformanceService

    if f.party_id is None:
        raise ValueError("Alt yüklenici performans raporu için firma seçin.")
    as_of = _as_of(f)
    perf = PerformanceService(conn).evaluate(f.party_id, as_of)
    rows = [[f"{c.project} — {c.title}", i.label, format_tr(i.value, 2) + " " + i.unit if i.measured else "Veri yok", i.basis, i.detail]
            for c in perf.contracts if not f.project_id or c.project_id == f.project_id for i in c.indicators]
    t = Table([Col("Sözleşme", width=2), Col("Gösterge", width=1.4), Col("Değer", ColKind.TEXT, 0.8), Col("Formül", width=2.6),
               Col("Hesapta kullanılan veri / veri yok nedeni", width=2.6)], rows or [["—", "Taşeron sözleşmesi yok", "Veri yok", "", ""]])
    notes = Table([Col("Tarih", width=0.8), Col("Puan", ColKind.TEXT, 0.5), Col("Değerlendiren", width=1.2), Col("Not", width=4)],
                  [[format_date_tr(n["rated_at"][:10]), f"{n['rating']}/5", n["rated_by"], n["note"]] for n in perf.notes] or [["—", "", "", "Not girilmemiş"]])
    return ReportDoc("Alt Yüklenici Performans Raporu", _meta_ctl(conn, f, as_of, "—", "Göstergeler kayıttan hesaplanır; puan üretilmez"),
                     [Section("Göstergeler", t), Section("Elle girilen değerlendirme notları", notes)],
                     [perf.coverage, "Eksik veriden değer üretilmez: veri yoksa \"Veri yok\" yazılır. Hakediş düzeltme oranı resmî uyuşmazlık kaydı değildir.",
                      "Kullanıcı notları değerlendirme kaydıdır; göstergelerden türetilmez ve sonradan değiştirilemez."])


def stock_ledger_report(conn, f: Filters) -> ReportDoc:
    from yapimmaliyet.services.stock_service import MOVE_LABELS, StockService

    pid = _project(f, "Stok hareket defteri")
    as_of = _as_of(f)
    led = [m for m in StockService(conn).ledger(pid) if f.in_range(m.date)]
    t = Table([Col("Tarih", width=0.8), Col("Hareket", width=1.1), Col("Sipariş", width=0.9), Col("Kalem", width=1.8), Col("Birim", width=0.5),
               Col("Miktar", Q, 0.8), Col("Stoğa etkisi", Q, 0.8), Col("Nereden", width=1.1), Col("Nereye", width=1.1), Col("Belge", width=0.8),
               Col("Not", width=1.6)],
              [[format_date_tr(m.date), MOVE_LABELS[m.kind], m.order_no, m.description, m.unit, m.quantity, m.effect, m.from_name, m.to_name,
                m.doc_no, m.note] for m in led],
              ["Toplam stok etkisi", "", "", "", "", None, sum((m.effect for m in led), ZERO), "", "", "", ""])
    return ReportDoc("Stok Hareket Defteri", _meta_ctl(conn, f, as_of, "—", "Miktar hareketleri; maliyet teslimde tanınır"),
                     [Section("Teslim girişleri, çıkışlar, iadeler, transferler ve sayım düzeltmeleri", t)],
                     ["Stok hareketleri yalnız miktardır; gerçekleşen maliyet, taahhüt ve EAC bu hareketlerden etkilenmez (teslim = maliyet).",
                      "Transfer toplam stoğu değiştirmez (etki 0). Hareket kayıtları değiştirilemez; düzeltme ters hareket ya da gerekçeli sayım düzeltmesiyle yapılır."])


EXTRA3 = [
    ReportDef("STOK_HAREKET", "Stok hareket defteri", "Satın Alma", ("project", "dates"), stock_ledger_report),
    ReportDef("ALT_YUK_PERFORMANS", "Alt yüklenici performans raporu", "Teklifler ve Alt Yükleniciler", ("party", "project", "dates"), performance_report),
    ReportDef("EVM_FAALIYET", "Faaliyet bazlı plan/gerçekleşme (PV, EV, AC, CPI, SPI)", "Proje Kontrolü", ("project", "dates", "discipline"), evm_report),
    ReportDef("FAALIYET_KESIF", "Faaliyet–keşif eşleme", "Proje Kontrolü", ("project",), scope_report),
    ReportDef("MALIYET_MUTABAKAT", "Maliyet kaynağı mutabakatı", "Proje Kontrolü", ("project", "dates"), reconciliation_report),
    ReportDef("TAAHHUT_DURUM", "Taahhüt durum raporu", "Proje Kontrolü", ("project", "dates"), commitment_report),
    ReportDef("EAC_KARLILIK", "EAC ve kârlılık", "Proje Kontrolü", ("project", "dates"), eac_report),
    ReportDef("PLAN_REVIZYON", "Plan revizyon karşılaştırması", "Proje Kontrolü", ("project",), revision_report),
    ReportDef("VERI_KALITE", "Veri kalite raporu", "Proje Kontrolü", ("project", "dates"), quality_report),
]
