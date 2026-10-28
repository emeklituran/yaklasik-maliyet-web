"""Ana sayfa panosunun verileri: yalnız kayıtlı işlemlerden hesaplanır, tahmin ya da örnek değer üretmez.

- Göstergeler: aktif proje, açık keşif, yaklaşık maliyet ve teklif hesabı, süren hakediş, teslim bekleyen sipariş.
- Dağılım: kapsamdaki keşiflerin son kesinleşmiş yaklaşık maliyet kaydındaki iş grubu tutarları (KDV hariç).
- Aylık seri: gerçekleşen maliyet kayıtları (harcama) ve geçerli idari hakedişler (bu dönem tutarı).
- Son işlemler: değişiklik günlüğünün son kayıtları.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.services.calc_service import CalcService
from yapimmaliyet.services.order_service import OrderService
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService

ZERO = Decimal(0)
ENTITIES = {
    "project": "Proje", "estimate": "Keşif", "estimate_item": "Keşif kalemi", "calculation": "Yaklaşık maliyet",
    "cost_sheet": "Firma maliyeti", "pp_payment": "Hakediş", "pp_contract": "Sözleşme",
    "pp_contract_change": "Sözleşme değişikliği", "pp_item": "Sözleşme kalemi", "price_book": "Fiyat kitabı",
    "analysis_book": "Analiz kitabı", "po_order": "Sipariş", "po_delivery": "Teslim",
    "purchase_request": "Satın alma talebi", "purchase_offer": "Satın alma teklifi", "ac_cost_entry": "Maliyet kaydı",
    "ac_budget": "Bütçe", "fa_final_account": "Kesin hesap", "tender_bid": "İhale teklifi", "party": "Firma",
    "custom_position": "Özel poz", "index_value": "Endeks değeri", "index_series": "Endeks serisi",
    "ws_plan_line": "İş programı", "new_price_record": "Yeni birim fiyat", "exchange_rate": "Döviz kuru",
    "estimate_comparison": "Keşif karşılaştırması", "position_price": "Poz fiyatı", "authority": "İdare",
}
ACTIONS = {"EKLE": "eklendi", "GUNCELLE": "güncellendi", "SIL": "silindi", "TOPLU": "toplu işlendi"}


@dataclass(frozen=True)
class Activity:
    at: str          # ISO zaman
    text: str


@dataclass
class Overview:
    active_projects: int = 0
    open_estimates: int = 0
    cost_calcs: int = 0
    offer_calcs: int = 0
    running_payments: int = 0
    open_orders: int = 0
    distribution: list[tuple[str, Decimal]] = field(default_factory=list)
    distribution_total: Decimal = ZERO
    months: list[str] = field(default_factory=list)          # "2026-03"
    spending: list[Decimal] = field(default_factory=list)
    payments: list[Decimal] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)


def month_window(keys: set[str], count: int | None, today: date | None = None) -> list[str]:
    """Verinin son ayına (yoksa bugüne) kadar ardışık `count` ay; None ise ilk kayıttan son kayda kadar."""
    last = max(keys) if keys else (today or date.today()).strftime("%Y-%m")
    y, m = int(last[:4]), int(last[5:7])
    if count is None:
        first = min(keys) if keys else last
        count = (y - int(first[:4])) * 12 + m - int(first[5:7]) + 1
    out = []
    for _ in range(max(count, 1)):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


class DashboardService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def overview(self, project_id: int | None = None, months: int | None = 12) -> Overview:
        c, pid = self.conn, project_id
        o = Overview()
        o.active_projects = c.execute("SELECT count(*) FROM projects WHERE status IN ('TASLAK','DEVAM')"
                                      " AND (? IS NULL OR id=?)", (pid, pid)).fetchone()[0]
        o.open_estimates = c.execute("SELECT count(*) FROM estimates WHERE locked=0 AND (? IS NULL OR project_id=?)",
                                     (pid, pid)).fetchone()[0]
        kinds = dict(c.execute("SELECT c.kind, count(*) FROM calculations c JOIN estimates e ON e.id=c.estimate_id"
                               " WHERE (? IS NULL OR e.project_id=?) GROUP BY c.kind", (pid, pid)).fetchall())
        o.cost_calcs, o.offer_calcs = kinds.get("YAKLASIK", 0), kinds.get("TEKLIF", 0)
        o.running_payments = c.execute(
            "SELECT count(*) FROM pp_payments p JOIN pp_contracts k ON k.id=p.contract_id"
            " WHERE p.status IN ('TASLAK','KONTROL') AND (? IS NULL OR k.project_id=?)", (pid, pid)).fetchone()[0]
        o.open_orders = sum(not (x.cancelled or x.complete) for x in OrderService(c).orders(pid))
        self._distribution(o, pid)
        self._monthly(o, pid, months)
        o.activity = self.activity()
        return o

    def _distribution(self, o: Overview, pid: int | None) -> None:
        calc = CalcService(self.conn)
        groups: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for (estimate_id,) in self.conn.execute("SELECT id FROM estimates WHERE (? IS NULL OR project_id=?)",
                                                (pid, pid)):
            snaps = [s for s in calc.snapshots(estimate_id) if s.kind == "YAKLASIK"]
            if not snaps:
                continue
            payload = calc.snapshot_payload(max(snaps, key=lambda s: s.id).id)
            for g in payload.get("groups", []):
                groups[g.get("name") or "Diğer"] += to_decimal(g["amount"])
            o.distribution_total += to_decimal(payload["totals"]["total_excl_vat"])
        o.distribution = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)

    def _monthly(self, o: Overview, pid: int | None, months: int | None) -> None:
        spend: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for day, amount in self.conn.execute("SELECT entry_date, amount FROM ac_cost_entries"
                                             " WHERE (? IS NULL OR project_id=?)", (pid, pid)):
            spend[day[:7]] += to_decimal(amount)
        paid: dict[str, Decimal] = defaultdict(lambda: ZERO)
        pay = ProgressPaymentService(self.conn)
        for (contract_id,) in self.conn.execute("SELECT id FROM pp_contracts WHERE kind='IDARI'"
                                                " AND (? IS NULL OR project_id=?)", (pid, pid)):
            for payment, payload in pay.effective_snapshots(contract_id):
                paid[payment.period_end[:7]] += to_decimal(payload["totals"]["this_total"])
        o.months = month_window(set(spend) | set(paid), months)
        o.spending = [spend.get(m, ZERO) for m in o.months]
        o.payments = [paid.get(m, ZERO) for m in o.months]

    def activity(self, limit: int = 6) -> list[Activity]:
        rows = self.conn.execute("SELECT at, entity, action, note FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for at, entity, action, note in rows:
            text = note or f"{ENTITIES.get(entity, entity)} {ACTIONS.get(action, action.lower())}"
            out.append(Activity(at, text))
        return out
