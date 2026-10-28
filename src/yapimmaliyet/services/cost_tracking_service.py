"""Gerçekleşen maliyet, bütçe karşılaştırması ve nakit akışı (proje bazında).

Tek veri kaynağı: gerçekleşen maliyet üç kaynaktan okunur, hiçbiri kopyalanmaz.
- Gider kayıtları (`ac_cost_entries`): elle girilen malzeme, işçilik, makine, genel gider vb.
- Satın alma teslimleri (`po_deliveries`): teslim tutarı (ek gider payı dahil), Malzeme türünde, teslim tarihinde.
- Taşeron hakedişleri: projenin taşeron sözleşmelerindeki geçerli (onaylı/ödendi) hakedişlerin dönem tutarı
  (iş + fiyat farkı, KDV hariç), Taşeron türünde, dönem sonunda.
Maliyet tutarları KDV hariçtir. Nakit akışı KDV dahil gerçek ödeme/tahsilat tutarlarıyla ve ödeme tarihine göredir:
- Giriş: projenin idari sözleşmelerinde "ödendi" işaretli hakedişlerin ödenecek tutarı (hakediş tarihinde).
- Çıkış: ödeme tarihi girilmiş gider kayıtları ve teslimler (tutar + KDV), "ödendi" taşeron hakedişlerinin ödenecek
  tutarı. Ödenmemişler alacak / borç olarak ayrıca gösterilir.
Kâr = kazanılmış hakediş geliri (idari geçerli hakedişlerin dönem tutarları toplamı) − gerçekleşen maliyet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.db.repositories import progress as pp
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.order_service import OrderService
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService
from yapimmaliyet.services.schedule_service import ScheduleService
from yapimmaliyet.services.validators import clean, require, validate_iso_date

CATEGORIES = {"MALZEME": "Malzeme", "ISCILIK": "İşçilik", "MAKINE": "Makine / ekipman", "TASERON": "Taşeron",
              "GENEL_GIDER": "Genel gider", "DIGER": "Diğer"}
ZERO = Decimal(0)


@dataclass(frozen=True)
class CostEntry:
    id: int
    project_id: int
    entry_date: str
    category: str
    description: str
    party: str | None
    document_no: str | None
    amount: Decimal
    vat_rate: Decimal
    paid_date: str | None
    notes: str | None
    activity_id: int | None = None

    @property
    def vat(self) -> Decimal:
        return quantize(self.amount * self.vat_rate, 2)


@dataclass(frozen=True)
class ActualLine:
    date: str
    category: str
    source: str                 # Gider kaydı | Satın alma teslimi | Taşeron hakedişi
    description: str
    amount: Decimal             # KDV hariç maliyet
    cash: Decimal               # KDV dahil ödeme tutarı (taşeronda ödenecek net)
    paid_date: str | None
    entry_id: int | None = None  # elle girilmiş gider kaydının kimliği (teslim / taşeron satırlarında yok)
    source_type: str = ""        # GIDER | TESLIM | TASERON_HAKEDIS (tahsis için kaynak türü)
    source_id: int | None = None  # kaynak kaydın kimliği (gider, teslim ya da hakediş)


@dataclass
class BudgetRow:
    category: str
    budget: Decimal | None
    actual: Decimal

    @property
    def variance(self) -> Decimal | None:
        return None if self.budget is None else self.budget - self.actual

    @property
    def ratio(self) -> Decimal | None:
        return self.actual / self.budget if self.budget else None


@dataclass
class CostReport:
    rows: list[BudgetRow]
    lines: list[ActualLine]
    contract_amount: Decimal    # idari sözleşmelerin toplam güncel bedeli
    earned: Decimal             # kazanılmış hakediş geliri

    @property
    def budget_total(self) -> Decimal:
        return sum((r.budget or ZERO for r in self.rows), ZERO)

    @property
    def actual_total(self) -> Decimal:
        return sum((r.actual for r in self.rows), ZERO)

    @property
    def margin(self) -> Decimal:
        return self.earned - self.actual_total


@dataclass(frozen=True)
class CashRow:
    period: str
    inflow: Decimal
    outflow: Decimal
    net: Decimal
    cumulative: Decimal
    planned_work: Decimal


@dataclass
class CashFlow:
    rows: list[CashRow]
    receivable: Decimal          # onaylı, ödendi işaretlenmemiş idari hakedişler
    payable: Decimal             # ödenmemiş gider, teslim ve taşeron hakedişleri
    notes: list[str] = field(default_factory=list)

    @property
    def inflow_total(self) -> Decimal:
        return sum((r.inflow for r in self.rows), ZERO)

    @property
    def outflow_total(self) -> Decimal:
        return sum((r.outflow for r in self.rows), ZERO)


def _money(value, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçerli bir sayı değil: {value}") from exc
    if d < 0 or (d == 0 and not allow_zero):
        raise ValidationError(f"{label} sıfırdan büyük olmalıdır." if not allow_zero else f"{label} negatif olamaz.")
    if d != quantize(d, 2):
        raise ValidationError(f"{label} en fazla 2 ondalık basamak içerebilir.")
    return d


class CostTrackingService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.pay = ProgressPaymentService(conn)
        self.orders = OrderService(conn)
        self.schedule = ScheduleService(conn)

    # --- gider kayıtları ---------------------------------------------------------
    def entries(self, project_id: int) -> list[CostEntry]:
        return [CostEntry(r["id"], r["project_id"], r["entry_date"], r["category"], r["description"], r["party"],
                          r["document_no"], from_db_text(r["amount"]), from_db_text(r["vat_rate"]), r["paid_date"],
                          r["notes"], r["activity_id"]) for r in self.conn.execute(
            "SELECT * FROM ac_cost_entries WHERE project_id = ? ORDER BY entry_date, id", (project_id,))]

    def _entry_data(self, data: dict) -> dict:
        if data.get("category") not in CATEGORIES:
            raise ValidationError("Gider türü seçilmelidir.")
        vat = data.get("vat_rate", "0.20")
        try:
            vat = to_decimal(vat)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"KDV oranı geçerli değil: {vat}") from exc
        if not 0 <= vat <= 1:
            raise ValidationError("KDV oranı 0 ile 1 arasında (ör. 0,20) olmalıdır.")
        d = {"entry_date": validate_iso_date(data.get("entry_date"), "Gider tarihi", required=True),
             "category": data["category"], "description": require(data.get("description"), "Açıklama"),
             "party": clean(data.get("party")), "document_no": clean(data.get("document_no")),
             "amount": to_db_text(_money(data.get("amount"), "Tutar"), 2), "vat_rate": to_db_text(vat, 4),
             "paid_date": validate_iso_date(data.get("paid_date"), "Ödeme tarihi"), "notes": clean(data.get("notes"))}
        if d["paid_date"] and d["paid_date"] < d["entry_date"]:
            raise ValidationError("Ödeme tarihi gider tarihinden önce olamaz.")
        if "activity_id" in data:
            d["activity_id"] = data["activity_id"] or None
        return d

    def add_entry(self, project_id: int, data: dict) -> int:
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError("Proje bulunamadı.")
        d = self._entry_data(data)
        self._check_activity(project_id, d.get("activity_id"))
        with transaction(self.conn):
            eid = self.conn.execute(f"INSERT INTO ac_cost_entries(project_id, {', '.join(d)}) VALUES (?, "
                                    f"{', '.join('?' for _ in d)})", [project_id, *d.values()]).lastrowid
            if "activity_id" in d:
                self._sync_alloc(eid, d["activity_id"], None)
            audit.log(self.conn, "ac_cost_entry", eid, "EKLE", new={"project": project_id, "amount": d["amount"],
                                                                    "category": d["category"]})
        return eid

    def _sync_alloc(self, entry_id: int, new_activity: int | None, old_activity: int | None) -> None:
        """Gider formundaki tek faaliyet seçimi = %100 tahsis. Değişmediyse (ör. bölünmüş tahsis varsa) dokunulmaz."""
        if new_activity == old_activity:
            return
        self.conn.execute("DELETE FROM cost_activity_allocations WHERE source_type = 'GIDER' AND source_id = ?", (entry_id,))
        if new_activity:
            self.conn.execute("INSERT INTO cost_activity_allocations(source_type, source_id, activity_id, ratio, note)"
                              " VALUES ('GIDER', ?, ?, '100.00', 'Gider formundan')", (entry_id, new_activity))

    def _check_activity(self, project_id: int, activity_id: int | None) -> None:
        if activity_id is not None and self.conn.execute(
                "SELECT 1 FROM ws_activities WHERE id = ? AND project_id = ?", (activity_id, project_id)).fetchone() is None:
            raise ValidationError("Seçilen faaliyet bu projeye ait değil.")

    def _get(self, entry_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM ac_cost_entries WHERE id = ?", (entry_id,)).fetchone()
        if r is None:
            raise NotFoundError("Gider kaydı bulunamadı.")
        return r

    def update_entry(self, entry_id: int, data: dict) -> None:
        old = self._get(entry_id)
        d = self._entry_data(data)
        self._check_activity(old["project_id"], d.get("activity_id"))
        with transaction(self.conn):
            self.conn.execute(f"UPDATE ac_cost_entries SET {', '.join(k + ' = ?' for k in d)} WHERE id = ?",
                              [*d.values(), entry_id])
            if "activity_id" in d:
                self._sync_alloc(entry_id, d["activity_id"], old["activity_id"])
            audit.log(self.conn, "ac_cost_entry", entry_id, "GUNCELLE", old={"amount": old["amount"]},
                      new={"amount": d["amount"]})

    def delete_entry(self, entry_id: int) -> None:
        old = self._get(entry_id)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM ac_cost_entries WHERE id = ?", (entry_id,))
            audit.log(self.conn, "ac_cost_entry", entry_id, "SIL", old={"amount": old["amount"],
                                                                        "description": old["description"]})

    # --- bütçe --------------------------------------------------------------------
    def budgets(self, project_id: int) -> dict[str, Decimal]:
        return {r[0]: from_db_text(r[1]) for r in self.conn.execute(
            "SELECT category, amount FROM ac_budgets WHERE project_id = ?", (project_id,))}

    def set_budget(self, project_id: int, category: str, amount) -> None:
        if category not in CATEGORIES:
            raise ValidationError("Gider türü seçilmelidir.")
        with transaction(self.conn):
            if amount in (None, ""):
                self.conn.execute("DELETE FROM ac_budgets WHERE project_id = ? AND category = ?", (project_id, category))
            else:
                a = _money(amount, "Bütçe tutarı", allow_zero=True)
                self.conn.execute("INSERT INTO ac_budgets(project_id, category, amount) VALUES (?, ?, ?) ON CONFLICT"
                                  "(project_id, category) DO UPDATE SET amount = excluded.amount",
                                  (project_id, category, to_db_text(a, 2)))
            audit.log(self.conn, "ac_budget", project_id, "GUNCELLE", new={"category": category, "amount": str(amount)})

    # --- birleşik gerçekleşen ------------------------------------------------------
    def _contracts(self, project_id: int, kind: str) -> list[pp.Contract]:
        return [c for c in pp.list_contracts(self.conn, kind) if c.project_id == project_id]

    def actual_lines(self, project_id: int) -> list[ActualLine]:
        out = [ActualLine(e.entry_date, e.category, "Gider kaydı", e.description + (f" ({e.party})" if e.party else ""),
                          e.amount, e.amount + e.vat, e.paid_date, e.id, "GIDER", e.id) for e in self.entries(project_id)]
        for o, d in self.orders.project_deliveries(project_id):
            out.append(ActualLine(d.delivery_date, "MALZEME", "Satın alma teslimi",
                                  f"Sipariş {o.order_no} — {o.supplier}" + (f", irsaliye {d.waybill_no}"
                                                                          if d.waybill_no else ""),
                                  d.amount, d.amount + d.vat, d.paid_date, None, "TESLIM", d.id))
        for c in self._contracts(project_id, "TASERON"):
            for p, snap in self.pay.effective_snapshots(c.id):
                out.append(ActualLine(p.period_end, "TASERON", "Taşeron hakedişi", f"{c.contractor} — {p.label}",
                                      to_decimal(snap["totals"]["this_total"]), to_decimal(snap["totals"]["payable"]),
                                      (p.payment_date or p.period_end) if p.status == "ODENDI" else None, None,
                                      "TASERON_HAKEDIS", p.id))
        return sorted(out, key=lambda x: (x.date, x.source))

    def cost_report(self, project_id: int) -> CostReport:
        lines = self.actual_lines(project_id)
        budgets = self.budgets(project_id)
        rows = [BudgetRow(k, budgets.get(k), sum((ln.amount for ln in lines if ln.category == k), ZERO))
                for k in CATEGORIES]
        from yapimmaliyet.services.contract_service import ContractService

        cs = ContractService(self.conn)
        contract_amount = earned = ZERO
        for c in self._contracts(project_id, "IDARI"):
            contract_amount += cs.summary(c.id).current_amount
            earned += sum((to_decimal(s["totals"]["work_period"]) + to_decimal(s["totals"]["price_diff_period"])
                           for _, s in self.pay.effective_snapshots(c.id)), ZERO)
        return CostReport(rows, lines, contract_amount, earned)

    def cash_flow(self, project_id: int) -> CashFlow:
        flows: dict[str, list[Decimal]] = {}   # dönem → [giriş, çıkış, planlanan iş]

        def add(day: str, i: int, amount: Decimal) -> None:
            flows.setdefault(day[:7], [ZERO, ZERO, ZERO])[i] += amount

        receivable = payable = ZERO
        for c in self._contracts(project_id, "IDARI"):
            for p, snap in self.pay.effective_snapshots(c.id):
                amount = to_decimal(snap["totals"]["payable"])
                if p.status == "ODENDI":
                    add(p.payment_date or p.period_end, 0, amount)
                else:
                    receivable += amount
            for pl in self.schedule.plan(c.id):
                add(pl.period, 2, pl.amount)
        for ln in self.actual_lines(project_id):
            if ln.paid_date:
                add(ln.paid_date, 1, ln.cash)
            else:
                payable += ln.cash
        rows, cum = [], ZERO
        for per in sorted(flows):
            i, o, plan = flows[per]
            cum += i - o
            rows.append(CashRow(per, i, o, i - o, cum, plan))
        cf = CashFlow(rows, receivable, payable)
        if receivable:
            cf.notes.append(f"Onaylı ama ödendi işaretlenmemiş idari hakediş alacağı: {receivable} TL.")
        if payable:
            cf.notes.append(f"Ödenmemiş gider / teslim / taşeron borcu: {payable} TL.")
        return cf
