"""Kredi ve borç yönetimi: krediler, kredi kartları, KMH ve elden borçlar; ödemeler, kapatma planı ve
yaklaşan ödeme takvimi (kredi taksitleri + cari vadeler + çek/senet vadeleri tek listede).
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain import debt as engine
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.cari_service import CariService
from yapimmaliyet.services.note_service import DIRECTIONS, KINDS as NOTE_KINDS, NoteService
from yapimmaliyet.services.treasury_service import insert_move, money
from yapimmaliyet.services.validators import clean, require, validate_iso_date

KINDS = {"KREDI": "Kredi", "KART": "Kredi kartı", "KMH": "Kredili mevduat (KMH)", "ELDEN": "Elden / diğer borç"}
ZERO = Decimal(0)


@dataclass(frozen=True)
class Debt:
    id: int
    kind: str
    name: str
    lender: str
    principal: Decimal
    balance: Decimal
    annual_rate: Decimal
    monthly_payment: Decimal
    credit_limit: Decimal
    pay_day: int
    start_date: str
    notes: str
    active: bool
    paid_total: Decimal = ZERO
    interest_total: Decimal = ZERO

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind]

    @property
    def closed(self) -> bool:
        return self.balance <= 0

    @property
    def usage(self) -> Decimal | None:
        """Kart / KMH limit kullanım oranı (yüzde)."""
        return self.balance * 100 / self.credit_limit if self.credit_limit > 0 else None

    def next_due(self, today: date) -> date:
        day = min(self.pay_day, calendar.monthrange(today.year, today.month)[1])
        d = today.replace(day=day)
        if d < today:
            y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            d = date(y, m, min(self.pay_day, calendar.monthrange(y, m)[1]))
        return d


@dataclass(frozen=True)
class Payment:
    id: int
    debt_id: int
    pay_date: str
    amount: Decimal
    interest: Decimal
    principal: Decimal


@dataclass(frozen=True)
class Upcoming:
    due: str
    source: str          # Kredi / borç | Cari | Çek / senet
    title: str
    amount: Decimal
    incoming: bool       # True: tahsilat (giriş), False: ödeme (çıkış)


class DebtService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def debts(self, include_closed: bool = True) -> list[Debt]:
        out = []
        for r in self.conn.execute("SELECT * FROM fin_debts ORDER BY active DESC, kind, name"):
            pays = self.payments(r["id"])
            d = Debt(r["id"], r["kind"], r["name"], r["lender"], from_db_text(r["principal"]),
                     from_db_text(r["balance"]), from_db_text(r["annual_rate"]), from_db_text(r["monthly_payment"]),
                     from_db_text(r["credit_limit"]), r["pay_day"], r["start_date"], r["notes"], bool(r["active"]),
                     sum((p.amount for p in pays), ZERO), sum((p.interest for p in pays), ZERO))
            if include_closed or (d.active and not d.closed):
                out.append(d)
        return out

    def debt(self, debt_id: int) -> Debt:
        d = next((x for x in self.debts() if x.id == debt_id), None)
        if d is None:
            raise NotFoundError("Borç kaydı bulunamadı.")
        return d

    def _values(self, kind, name, principal, annual_rate, monthly_payment, credit_limit, pay_day, start_date,
                term_months):
        if kind not in KINDS:
            raise ValidationError("Geçersiz borç türü.")
        name = require(name, "Borç adı")
        principal = money(principal, "Tutar", positive=True)
        rate = money(annual_rate, "Yıllık faiz oranı")
        if rate > 1000:
            raise ValidationError("Yıllık faiz oranı yüzde olarak girilmelidir (ör. 48).")
        payment = money(monthly_payment, "Aylık ödeme")
        if payment == 0 and kind == "KREDI" and term_months:
            payment = engine.annuity(principal, rate, int(term_months))
        limit = money(credit_limit, "Limit")
        if not 1 <= int(pay_day) <= 31:
            raise ValidationError("Ödeme günü 1–31 arasında olmalıdır.")
        start_date = validate_iso_date(start_date, "Başlangıç tarihi", required=True)
        return kind, name, principal, rate, payment, limit, int(pay_day), start_date

    def create(self, kind: str, name: str, principal, annual_rate="0", monthly_payment="0", credit_limit="0",
               pay_day: int = 1, start_date: str | None = None, lender: str = "", notes: str = "",
               term_months: int | None = None, balance=None) -> int:
        kind, name, principal, rate, payment, limit, pay_day, start_date = self._values(
            kind, name, principal, annual_rate, monthly_payment, credit_limit, pay_day,
            start_date or date.today().isoformat(), term_months)
        bal = principal if balance in (None, "") else money(balance, "Kalan borç")
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO fin_debts(kind, name, lender, principal, balance, annual_rate, monthly_payment, "
                "credit_limit, pay_day, start_date, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, name, clean(lender) or "", to_db_text(principal, 2), to_db_text(bal, 2), to_db_text(rate, 4),
                 to_db_text(payment, 2), to_db_text(limit, 2), pay_day, start_date, clean(notes) or ""))
            audit.log(self.conn, "fin_debt", cur.lastrowid, "EKLE", new={"kind": kind, "name": name,
                                                                         "principal": str(principal)})
        return cur.lastrowid

    def update(self, debt_id: int, kind: str, name: str, principal, annual_rate, monthly_payment, credit_limit,
               pay_day: int, start_date: str, lender: str = "", notes: str = "", balance=None) -> None:
        old = self.debt(debt_id)
        kind, name, principal, rate, payment, limit, pay_day, start_date = self._values(
            kind, name, principal, annual_rate, monthly_payment, credit_limit, pay_day, start_date, None)
        bal = old.balance if balance in (None, "") else money(balance, "Kalan borç")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE fin_debts SET kind=?, name=?, lender=?, principal=?, balance=?, annual_rate=?, "
                "monthly_payment=?, credit_limit=?, pay_day=?, start_date=?, notes=? WHERE id=?",
                (kind, name, clean(lender) or "", to_db_text(principal, 2), to_db_text(bal, 2), to_db_text(rate, 4),
                 to_db_text(payment, 2), to_db_text(limit, 2), pay_day, start_date, clean(notes) or "", debt_id))
            audit.log(self.conn, "fin_debt", debt_id, "GUNCELLE", old={"balance": str(old.balance)},
                      new={"name": name, "balance": str(bal)})

    def set_active(self, debt_id: int, active: bool) -> None:
        self.debt(debt_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE fin_debts SET active = ? WHERE id = ?", (int(active), debt_id))

    def delete(self, debt_id: int) -> None:
        d = self.debt(debt_id)
        with transaction(self.conn):
            moves = [r["move_id"] for r in self.conn.execute(
                "SELECT move_id FROM fin_debt_payments WHERE debt_id = ? AND move_id IS NOT NULL", (debt_id,))]
            self.conn.execute("DELETE FROM fin_debts WHERE id = ?", (debt_id,))
            for m in moves:
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (m,))
            audit.log(self.conn, "fin_debt", debt_id, "SIL", old={"name": d.name})

    # --- ödemeler ----------------------------------------------------------------
    def payments(self, debt_id: int) -> list[Payment]:
        return [Payment(r["id"], r["debt_id"], r["pay_date"], from_db_text(r["amount"]), from_db_text(r["interest"]),
                        from_db_text(r["principal"]))
                for r in self.conn.execute("SELECT * FROM fin_debt_payments WHERE debt_id = ? ORDER BY pay_date, id",
                                           (debt_id,))]

    def pay(self, debt_id: int, pay_date: str, amount, account_id: int | None = None) -> int:
        d = self.debt(debt_id)
        amount = money(amount, "Ödeme tutarı", positive=True)
        pay_date = validate_iso_date(pay_date, "Ödeme tarihi", required=True)
        if d.closed:
            raise ValidationError("Bu borç kapanmış; ödeme girilemez.")
        interest, principal = engine.split_payment(d.balance, d.annual_rate, amount)
        if interest + principal < amount:
            raise ValidationError(f"Ödeme kalan borçtan ve dönem faizinden fazla. En çok "
                                  f"{to_db_text(interest + principal, 2)} ödenebilir.")
        with transaction(self.conn):
            move_id = None
            if account_id:
                move_id = insert_move(self.conn, account_id, pay_date, -amount, f"{KINDS[d.kind]} ödemesi: {d.name}",
                                      "BORC")
            cur = self.conn.execute("INSERT INTO fin_debt_payments(debt_id, pay_date, amount, interest, principal, "
                                    "move_id) VALUES (?, ?, ?, ?, ?, ?)",
                                    (debt_id, pay_date, to_db_text(amount, 2), to_db_text(interest, 2),
                                     to_db_text(principal, 2), move_id))
            self.conn.execute("UPDATE fin_debts SET balance = ? WHERE id = ?",
                              (to_db_text(d.balance - principal, 2), debt_id))
            audit.log(self.conn, "fin_debt_payment", cur.lastrowid, "EKLE",
                      new={"debt": d.name, "amount": str(amount), "interest": str(interest)})
        return cur.lastrowid

    def delete_payment(self, payment_id: int) -> None:
        r = self.conn.execute("SELECT * FROM fin_debt_payments WHERE id = ?", (payment_id,)).fetchone()
        if r is None:
            raise NotFoundError("Ödeme bulunamadı.")
        d = self.debt(r["debt_id"])
        with transaction(self.conn):
            self.conn.execute("DELETE FROM fin_debt_payments WHERE id = ?", (payment_id,))
            if r["move_id"]:
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (r["move_id"],))
            self.conn.execute("UPDATE fin_debts SET balance = ? WHERE id = ?",
                              (to_db_text(d.balance + from_db_text(r["principal"]), 2), d.id))
            audit.log(self.conn, "fin_debt_payment", payment_id, "SIL", old={"amount": r["amount"]})

    # --- özet, plan, takvim -------------------------------------------------------
    def summary(self) -> dict[str, Decimal]:
        live = [d for d in self.debts(include_closed=False)]
        total = sum((d.balance for d in live), ZERO)
        load = sum((min(d.monthly_payment, d.balance) for d in live), ZERO)
        rate = sum((d.balance * d.annual_rate for d in live), ZERO) / total if total else ZERO
        monthly_interest = sum((engine.interest_for(d.balance, d.annual_rate) for d in live), ZERO)
        return {"total": total, "load": load, "rate": rate, "monthly_interest": monthly_interest,
                "count": Decimal(len(live))}

    def plan(self, budget, strategy: str) -> tuple[engine.PlanResult, engine.PlanResult]:
        """(seçilen strateji, yalnızca asgari ödeme) karşılaştırması."""
        inputs = [engine.DebtInput(d.id, d.name, d.balance, d.annual_rate, d.monthly_payment)
                  for d in self.debts(include_closed=False)]
        if not inputs:
            raise ValidationError("Açık borç yok.")
        budget = money(budget, "Aylık bütçe", positive=True)
        try:
            return engine.simulate(inputs, budget, strategy), engine.simulate(inputs, None, strategy)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def upcoming(self, days: int = 60, today: date | None = None) -> list[Upcoming]:
        today = today or date.today()
        end = today + timedelta(days=days)
        out: list[Upcoming] = []
        for d in self.debts(include_closed=False):
            if d.monthly_payment <= 0:
                continue
            due = d.next_due(today)
            while due <= end:
                out.append(Upcoming(due.isoformat(), "Kredi / borç", f"{d.name} ({d.kind_label})",
                                    min(d.monthly_payment, d.balance), False))
                due = d.next_due(due + timedelta(days=1))
        for i in CariService(self.conn).open_items():
            if i.due <= end.isoformat():
                out.append(Upcoming(i.due, "Cari", f"{i.party_name} · {i.entry.description}", i.remaining,
                                    i.receivable))
        for n in NoteService(self.conn).notes(open_only=True):
            if n.due_date <= end.isoformat():
                who = f" · {n.party_name}" if n.party_name else ""
                out.append(Upcoming(n.due_date, "Çek / senet",
                                    f"{DIRECTIONS[n.direction]} {NOTE_KINDS[n.kind].lower()} {n.number}{who}".strip(),
                                    n.amount, n.direction == "ALINAN"))
        return sorted(out, key=lambda u: u.due)
