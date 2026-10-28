"""Taahhüt edilen maliyet: sipariş ve taşeron sözleşmelerinden doğan yükümlülükler, gerçekleşen kısım ve açık taahhüt.

Kavramlar ayrıdır (şartname §11): taahhüt (sipariş / sözleşme ile doğar) · gerçekleşen (teslim / onaylı hakediş) · açık taahhüt
(taahhüt − gerçekleşen) · ödenen (nakit; maliyet değildir). Sipariş kendi başına maliyet değildir; teslim edilen kısım gerçekleşen
maliyet olarak zaten `actual_lines` içindedir ve burada ikinci kez gider yazılmaz — yalnız açık taahhütten düşülür.
- Sipariş taahhüdü = KDV hariç mal tutarı + ek gider; iptal edilen sipariş taahhüdü 0 (kayıt görünür kalır).
- Taşeron taahhüdü = sözleşmenin güncel bedeli (**yalnız onaylı** değişiklikler; onay bekleyenler bilgi olarak ayrıca gösterilir).
- Gerçekleşen = teslim tutarı / geçerli (ONAY, ODENDI) hakedişlerin dönem iş tutarı (fiyat farkı dahil `this_total`, AC ile aynı ölçü).
- Fazla teslim/hakediş (açık < 0) istisna olarak işaretlenir; toplam açık taahhütte negatif değer 0 alınır, istisna listelenir.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.services.contract_service import ContractService
from yapimmaliyet.services.order_service import OrderService
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService

ZERO = Decimal(0)


@dataclass
class Commitment:
    kind: str                    # SIPARIS | TASERON
    ref: str
    party: str
    committed: Decimal
    realized: Decimal
    paid: Decimal                # nakit (KDV dahil); maliyet değil
    source_id: int
    date: str | None = None
    note: str = ""
    flags: list[str] = field(default_factory=list)

    @property
    def open(self) -> Decimal:
        return self.committed - self.realized

    @property
    def open_effective(self) -> Decimal:
        return max(self.open, ZERO)


@dataclass
class CommitmentSummary:
    rows: list[Commitment]
    committed: Decimal
    realized: Decimal
    open: Decimal
    paid: Decimal
    exceptions: list[Commitment]
    pending_changes: Decimal      # onay bekleyen sözleşme değişikliği toplamı (taahhüde DAHİL DEĞİL)


class CommitmentService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.orders = OrderService(conn)
        self.contracts = ContractService(conn)
        self.pay = ProgressPaymentService(conn)

    def summary(self, project_id: int, as_of: str | None = None) -> CommitmentSummary:
        rows: list[Commitment] = []
        for o in self.orders.orders(project_id):
            if as_of and o.order_date > as_of:
                continue
            dels = [d for d in o.deliveries if not as_of or d.delivery_date <= as_of]
            realized = sum((d.amount for d in dels), ZERO)
            paid = sum((d.amount + d.vat for d in dels if d.paid_date and (not as_of or d.paid_date <= as_of)), ZERO)
            c = Commitment("SIPARIS", f"Sipariş {o.order_no}", o.supplier, ZERO if o.cancelled else o.total_excl_vat, realized,
                           paid, o.id, o.order_date, "İptal edildi: taahhüt 0" if o.cancelled else "")
            rows.append(c)
        pending = ZERO
        for r in self.conn.execute("SELECT id, title, contractor FROM pp_contracts WHERE kind = 'TASERON' AND project_id = ? ORDER BY id",
                                   (project_id,)):
            s = self.contracts.summary(r["id"])
            pays = [(p, snap) for p, snap in self.pay.effective_snapshots(r["id"]) if not as_of or p.period_end <= as_of]
            realized = sum((to_decimal(snap["totals"]["this_total"]) for _p, snap in pays), ZERO)
            paid = sum((to_decimal(snap["totals"]["payable"]) for p, snap in pays if p.status == "ODENDI"), ZERO)
            pend = sum((ch.amount_delta for ch in s.pending if ch.amount_delta), ZERO)
            pending += pend
            note = f"Onay bekleyen değişiklik {pend} (taahhüde dahil değil)" if pend else ""
            rows.append(Commitment("TASERON", f"Sözleşme: {r['title']}", r["contractor"], s.current_amount, realized, paid,
                                   r["id"], s.contract.contract_date, note))
        for c in rows:
            if c.open < 0:
                c.flags.append("Fazla gerçekleşme: açık taahhüt negatif (istisna)")
        return CommitmentSummary(rows, sum((c.committed for c in rows), ZERO), sum((c.realized for c in rows), ZERO),
                                 sum((c.open_effective for c in rows), ZERO), sum((c.paid for c in rows), ZERO),
                                 [c for c in rows if c.flags], pending)
