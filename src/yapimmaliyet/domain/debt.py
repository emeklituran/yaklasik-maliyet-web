"""Kredi ve borç hesapları: aylık faiz, eşit taksit, ödeme dağılımı ve borç kapatma simülasyonu.

Kurallar:
- Oranlar yıllık yüzde olarak girilir; aylık oran = yıllık / 1200 (vergi dahil oran girilmesi beklenir).
- Ödeme önce dönem faizine, kalanı anaparaya sayılır; anapara bakiyeden büyük olamaz.
- Simülasyon sabit aylık bütçeyle çalışır: her ay faiz işler, tüm borçların asgari ödemesi yapılır,
  artan tutar stratejiye göre seçilen borca gider; kapanan borcun taksiti sonraki borca aktarılır.
  ÇIĞ: en yüksek faizli borç önce (toplam faiz en az). KARTOPU: en küçük bakiyeli borç önce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.rounding import quantize

ZERO = Decimal(0)
MAX_MONTHS = 600
STRATEGIES = {"CIG": "Çığ (yüksek faiz önce)", "KARTOPU": "Kartopu (küçük borç önce)"}


def monthly_rate(annual_pct: Decimal) -> Decimal:
    return annual_pct / Decimal(1200)


def interest_for(balance: Decimal, annual_pct: Decimal) -> Decimal:
    return quantize(max(balance, ZERO) * monthly_rate(annual_pct), 2)


def annuity(principal: Decimal, annual_pct: Decimal, months: int) -> Decimal:
    """Eşit taksit tutarı."""
    if months <= 0:
        raise ValueError("Vade en az 1 ay olmalıdır.")
    r = monthly_rate(annual_pct)
    if r == 0:
        return quantize(principal / months, 2)
    f = (1 + r) ** months
    return quantize(principal * r * f / (f - 1), 2)


def split_payment(balance: Decimal, annual_pct: Decimal, amount: Decimal) -> tuple[Decimal, Decimal]:
    """Ödemeyi (faiz, anapara) olarak böler."""
    if amount <= 0:
        raise ValueError("Ödeme tutarı sıfırdan büyük olmalıdır.")
    interest = min(interest_for(balance, annual_pct), amount)
    principal = min(amount - interest, max(balance, ZERO))
    return interest, principal


@dataclass(frozen=True)
class DebtInput:
    key: int
    name: str
    balance: Decimal
    annual_pct: Decimal
    minimum: Decimal


@dataclass
class PlanRow:
    key: int
    name: str
    months: int = 0
    interest: Decimal = ZERO
    paid: Decimal = ZERO


@dataclass
class PlanResult:
    strategy: str
    months: int
    total_interest: Decimal
    total_paid: Decimal
    rows: list[PlanRow] = field(default_factory=list)   # kapanış sırasına göre
    finished: bool = True


def _order(debts: list[dict], strategy: str) -> list[dict]:
    if strategy == "CIG":
        return sorted(debts, key=lambda d: (-d["rate"], d["bal"]))
    return sorted(debts, key=lambda d: (d["bal"], -d["rate"]))


def simulate(debts: list[DebtInput], budget: Decimal | None, strategy: str) -> PlanResult:
    """Borç kapatma planı. `budget` None ise yalnızca asgari ödemeler yapılır (artan tutar yok)."""
    if strategy not in STRATEGIES:
        raise ValueError("Geçersiz strateji.")
    live = [{"d": d, "bal": d.balance, "rate": d.annual_pct, "row": PlanRow(d.key, d.name)}
            for d in debts if d.balance > 0]
    minimums = sum((d.minimum for d in debts if d.balance > 0), ZERO)
    if budget is not None and budget < minimums:
        raise ValueError("Aylık bütçe, borçların asgari ödemelerinin toplamından az olamaz.")
    done: list[PlanRow] = []
    month = 0
    total_interest = total_paid = ZERO
    while live and month < MAX_MONTHS:
        month += 1
        cash = budget if budget is not None else None
        for x in live:
            i = interest_for(x["bal"], x["rate"])
            x["bal"] += i
            x["row"].interest += i
            total_interest += i
        for x in live:   # asgari ödemeler
            pay = min(x["d"].minimum, x["bal"])
            x["bal"] -= pay
            x["row"].paid += pay
            total_paid += pay
            if cash is not None:
                cash -= pay
        if cash is not None and cash > 0:
            for x in _order([y for y in live if y["bal"] > 0], strategy):
                pay = min(cash, x["bal"])
                x["bal"] -= pay
                x["row"].paid += pay
                total_paid += pay
                cash -= pay
                if cash <= 0:
                    break
        for x in [y for y in live if y["bal"] <= 0]:
            x["row"].months = month
            done.append(x["row"])
            live.remove(x)
    for x in live:   # 50 yılda kapanmayan borç: ödeme faizi karşılamıyor
        x["row"].months = month
        done.append(x["row"])
    return PlanResult(strategy, month, total_interest, total_paid, done, finished=not live)
