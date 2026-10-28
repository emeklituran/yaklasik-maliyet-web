"""İş programı ve ilerleme: sözleşmenin aylık planlanan iş tutarları ile onaylı hakedişlerden gerçekleşen iş.

- Plan tutarı KDV ve fiyat farkı hariçtir; pursantaj = tutar / sözleşmenin güncel bedeli (yalnız onaylı
  değişikliklerle, `ContractService` ile aynı hesap).
- Gerçekleşen: geçerli (onaylı/ödendi) hakedişlerin kesinleşmiş kayıtlarındaki dönem iş tutarı, dönem sonunun ayına
  yazılır. Taslak ve kontroldeki hakedişler sayılmaz; düzeltilen revizyonun yerine geçerli revizyon sayılır.
- Sapma = gerçekleşen kümülatif − planlanan kümülatif (negatif: programın gerisinde).
- Eşit dağıtım: güncel bedel başlangıç ile güncel bitiş arasındaki aylara eşit bölünür; kuruş farkı son aya yazılır.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.contract_service import ContractService, ContractSummary
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService
from yapimmaliyet.services.validators import clean

ZERO = Decimal(0)
_PERIOD = re.compile(r"^(\d{4})-(\d{2})$")
_PERIOD_TR = re.compile(r"^(\d{1,2})[./](\d{4})$")


def parse_period(text: str | None) -> str:
    """'YYYY-AA' ya da 'AA.YYYY' → 'YYYY-AA'."""
    raw = (text or "").strip()
    m = _PERIOD.match(raw)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    else:
        m = _PERIOD_TR.match(raw)
        if not m:
            raise ValidationError(f"Dönem AA.YYYY biçiminde girilmelidir (ör. 03.2027): '{raw}'")
        month, year = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12 or not 1900 <= year <= 2200:
        raise ValidationError(f"Geçersiz dönem: '{raw}'")
    return f"{year:04d}-{month:02d}"


def period_label(period: str) -> str:
    return f"{period[5:7]}.{period[:4]}"


def months_between(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@dataclass(frozen=True)
class PlanLine:
    id: int
    period: str
    amount: Decimal
    note: str | None


@dataclass(frozen=True)
class ScheduleRow:
    period: str
    planned: Decimal
    planned_cum: Decimal
    planned_pct: Decimal | None
    actual: Decimal | None          # None: bu dönemde onaylı hakediş yok
    actual_cum: Decimal | None      # None: henüz gerçekleşme yok (son hakediş döneminden sonrası)
    actual_pct: Decimal | None
    deviation: Decimal | None
    note: str | None = None


@dataclass
class Schedule:
    summary: ContractSummary
    rows: list[ScheduleRow]
    plan_total: Decimal
    actual_total: Decimal
    warnings: list[str] = field(default_factory=list)

    @property
    def current_amount(self) -> Decimal:
        return self.summary.current_amount

    @property
    def last_deviation(self) -> Decimal | None:
        devs = [r.deviation for r in self.rows if r.deviation is not None]
        return devs[-1] if devs else None


class ScheduleService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractService(conn)
        self.payments = ProgressPaymentService(conn)

    def plan(self, contract_id: int) -> list[PlanLine]:
        return [PlanLine(r[0], r[1], from_db_text(r[2]), r[3]) for r in self.conn.execute(
            "SELECT id, period, amount, note FROM ws_plan_lines WHERE contract_id = ? ORDER BY period",
            (contract_id,))]

    @staticmethod
    def _amount(value) -> Decimal:
        try:
            d = to_decimal(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Planlanan tutar geçerli bir sayı değil: {value}") from exc
        if d < 0:
            raise ValidationError("Planlanan tutar negatif olamaz.")
        if d != quantize(d, 2):
            raise ValidationError("Planlanan tutar en fazla 2 ondalık basamak içerebilir.")
        return d

    def set_period(self, contract_id: int, period: str, amount, note: str | None = None) -> int:
        """Dönemin plan tutarını yazar (dönem varsa günceller)."""
        self.contracts.summary(contract_id)
        p, a = parse_period(period), self._amount(amount)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO ws_plan_lines(contract_id, period, amount, note) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(contract_id, period) DO UPDATE SET amount = excluded.amount, note = excluded.note",
                (contract_id, p, to_db_text(a, 2), clean(note)))
            lid = self.conn.execute("SELECT id FROM ws_plan_lines WHERE contract_id = ? AND period = ?",
                                    (contract_id, p)).fetchone()[0]
            audit.log(self.conn, "ws_plan_line", lid, "GUNCELLE", new={"contract": contract_id, "period": p,
                                                                         "amount": str(a)})
        return lid

    def delete_period(self, line_id: int) -> None:
        r = self.conn.execute("SELECT contract_id, period FROM ws_plan_lines WHERE id = ?", (line_id,)).fetchone()
        if r is None:
            raise NotFoundError("Plan satırı bulunamadı.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM ws_plan_lines WHERE id = ?", (line_id,))
            audit.log(self.conn, "ws_plan_line", line_id, "SIL", old={"contract": r[0], "period": r[1]})

    def distribute(self, contract_id: int, first: str | None = None, last: str | None = None) -> int:
        """Planı güncel bedelin aylara eşit dağıtımıyla değiştirir; ay sayısını döndürür."""
        s = self.contracts.summary(contract_id)
        first = parse_period(first) if first else (s.contract.start_date or "")[:7]
        last = parse_period(last) if last else (s.current_end or "")[:7]
        if not first or not last:
            raise ValidationError("Eşit dağıtım için sözleşmede başlangıç ve bitiş tarihi olmalı ya da ilk ve son "
                                  "dönem girilmelidir.")
        months = months_between(first, last)
        if not months:
            raise ValidationError("Son dönem ilk dönemden önce olamaz.")
        total = s.current_amount
        each = quantize(total / len(months), 2)
        amounts = [each] * (len(months) - 1) + [total - each * (len(months) - 1)]
        if amounts[-1] < 0:
            raise ValidationError("Eşit dağıtım yapılamadı (son ay negatif).")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM ws_plan_lines WHERE contract_id = ?", (contract_id,))
            for p, a in zip(months, amounts):
                self.conn.execute("INSERT INTO ws_plan_lines(contract_id, period, amount, note) VALUES (?, ?, ?, ?)",
                                  (contract_id, p, to_db_text(a, 2), "Eşit dağıtım"))
            audit.log(self.conn, "ws_plan_line", None, "TOPLU",
                      new={"contract": contract_id, "first": first, "last": last, "total": str(total)},
                      note="İş programı eşit dağıtım")
        return len(months)

    def actual_by_period(self, contract_id: int) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p, snap in self.payments.effective_snapshots(contract_id):
            key = p.period_end[:7]
            out[key] = out.get(key, ZERO) + to_decimal(snap["totals"]["work_period"])
        return out

    def build(self, contract_id: int, today: date | None = None) -> Schedule:
        s = self.contracts.summary(contract_id)
        plan = {pl.period: pl for pl in self.plan(contract_id)}
        actual = self.actual_by_period(contract_id)
        periods = sorted(set(plan) | set(actual))
        if periods:
            periods = months_between(periods[0], periods[-1])
        last_actual = max(actual) if actual else None
        base = s.current_amount
        pct = (lambda v: v / base if base else None)
        rows, pcum, acum = [], ZERO, ZERO
        for per in periods:
            planned = plan[per].amount if per in plan else ZERO
            pcum += planned
            a = actual.get(per)
            reached = last_actual is not None and per <= last_actual
            if reached:
                acum += a or ZERO
            rows.append(ScheduleRow(per, planned, pcum, pct(pcum), a, acum if reached else None,
                                    pct(acum) if reached else None, acum - pcum if reached else None,
                                    plan[per].note if per in plan else None))
        plan_total = sum((pl.amount for pl in plan.values()), ZERO)
        sched = Schedule(s, rows, plan_total, acum)
        if not plan:
            sched.warnings.append("İş programı girilmemiş; 'Dönem Ekle' ya da 'Eşit Dağıt' ile oluşturun.")
        elif plan_total != base:
            sched.warnings.append(f"Plan toplamı ({plan_total}) güncel sözleşme bedelinden ({base}) "
                                  f"{plan_total - base:+} farklı.")
        if s.pending:
            sched.warnings.append(f"{len(s.pending)} sözleşme değişikliği onay bekliyor; güncel bedele dahil değil.")
        dev = sched.last_deviation
        if dev is not None and dev < 0:
            sched.warnings.append(f"Son hakediş döneminde iş programının {-dev} TL gerisinde.")
        now = (today or date.today()).isoformat()[:7]
        overdue = [r for r in rows if r.period < now and r.period > (last_actual or "") and r.planned]
        if overdue:
            sched.warnings.append(f"Geçmiş {len(overdue)} planlı dönem için onaylı hakediş yok.")
        return sched
