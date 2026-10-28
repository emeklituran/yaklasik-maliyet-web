"""Kazanılmış değer yönetimi (EVM): BAC, PV, EV, AC, CV, SV, CPI, SPI.

Tanımlar (docs/TEKNIK_KARARLAR.md K9–K10; şartname §10):
- BAC: onaylı (kilitli) baz plan toplamı; baz plan yoksa güncel plan toplamı (uyarıyla); o da yoksa maliyet bütçesi; hiçbiri yoksa N/A.
- PV: baz plana göre as-of tarihine kadar planlanan değer (aylık dağılım; dönem içi kısmi ay = faaliyetin o aydaki gün oranı).
- EV: faaliyet planlanan maliyeti × as-of tarihindeki fiziksel ilerleme % (en son snapshot ≤ as-of); ilerleme yoksa 0 ve uyarı.
- AC: as-of tarihine kadar kaynak maliyet olayları (`actual_lines`, çifte sayım yok). Faaliyet AC'si tahsislerden; tahsis edilmemiş ayrıca gösterilir.
- CV = EV − AC; SV = EV − PV; CPI = EV / AC; SPI = EV / PV. Payda sıfırsa N/A (None) — 0 ya da sonsuz uydurulmaz.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.services.schedule_control_service import Baseline, ScheduleControlService

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def _overlap_days(start: date, end: date, lo: date, hi: date) -> int:
    a, b = max(start, lo), min(end, hi)
    return max((b - a).days + 1, 0)


def planned_value(start: str, end: str, monthly: dict[str, Decimal], as_of: str) -> Decimal:
    """Aylık plana göre as-of tarihine kadar planlanan değer. Tam geçmiş aylar tam, as-of ayı faaliyetin gün oranıyla kısmi."""
    s, e, d = date.fromisoformat(start), date.fromisoformat(end), date.fromisoformat(as_of)
    if d < s:
        return ZERO
    total = ZERO
    for period, amount in sorted(monthly.items()):
        y, m = int(period[:4]), int(period[5:7])
        first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        if d < first:
            continue
        if d >= last:
            total += amount
            continue
        active = _overlap_days(s, e, first, last)
        elapsed = _overlap_days(s, min(e, d), first, last)
        total += quantize(amount * elapsed / active, 2) if active else ZERO
    return quantize(total, 2)


@dataclass
class EvmRow:
    activity_id: int | None
    code: str
    title: str
    bac: Decimal
    pv: Decimal
    ev: Decimal
    ac: Decimal
    progress: Decimal | None

    @property
    def cpi(self) -> Decimal | None:
        return quantize(self.ev / self.ac, 2) if self.ac > 0 else None

    @property
    def spi(self) -> Decimal | None:
        return quantize(self.ev / self.pv, 2) if self.pv > 0 else None


@dataclass
class Evm:
    project_id: int
    as_of: str
    baseline: Baseline | None
    bac: Decimal | None
    bac_source: str
    pv: Decimal
    ev: Decimal
    ac: Decimal
    unallocated_ac: Decimal
    rows: list[EvmRow]
    warnings: list[str] = field(default_factory=list)
    series: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)   # dönem, aylık planlanan, aylık gerçekleşen
    method: str = "Manuel fiziksel yüzde; gün oranlı aylık plan"

    @property
    def cv(self) -> Decimal:
        return self.ev - self.ac

    @property
    def sv(self) -> Decimal:
        return self.ev - self.pv

    @property
    def cpi(self) -> Decimal | None:
        return quantize(self.ev / self.ac, 2) if self.ac > 0 else None

    @property
    def spi(self) -> Decimal | None:
        return quantize(self.ev / self.pv, 2) if self.pv > 0 else None


class EvmService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ctl = ScheduleControlService(conn)

    def evaluate(self, project_id: int, as_of: str | None = None, baseline_id: int | None = None) -> Evm:
        as_of = as_of or date.today().isoformat()
        warnings: list[str] = []
        base = self.ctl.baseline(baseline_id) if baseline_id else self.ctl.latest_locked(project_id)
        live = self.ctl.plan.activities(project_id)
        if base is not None:
            items = self.ctl.items(base.id)
            in_base = {i["activity_id"] for i in items}
            extra = [a.name for a in live if a.id not in in_base]
            if extra:
                warnings.append(f"Baz plana dahil olmayan {len(extra)} faaliyet var (güncel planda): " + ", ".join(extra[:5]))
        else:
            items = [{"activity_id": a.id, "external_code": a.external_code, "title": a.name, "planned_start": a.planned_start,
                      "planned_end": a.planned_end, "planned_cost": a.planned_cost, "monthly": self.ctl.plan.effective_months(a)}
                     for a in live]
            if items:
                warnings.append("Onaylı baz plan yok: güncel plan kullanıldı (PV/BAC kıyası için baz planı kilitleyin).")
        alloc = self.ctl.activity_allocated(project_id, as_of)
        rows, pv_total, ev_total = [], ZERO, ZERO
        missing_progress = []
        for it in items:
            pv = planned_value(it["planned_start"], it["planned_end"], it["monthly"], as_of)
            pct = self.ctl.progress_at(it["activity_id"], as_of) if it["activity_id"] is not None else None
            ev = quantize(it["planned_cost"] * (pct or ZERO) / HUNDRED, 2)
            if pct is None and it["planned_start"] <= as_of and it["planned_cost"] > 0:
                missing_progress.append(it["title"])
            ac = sum(alloc.get(it["activity_id"], {}).values(), ZERO) if it["activity_id"] is not None else ZERO
            rows.append(EvmRow(it["activity_id"], it["external_code"] or "", it["title"], it["planned_cost"], pv, ev, ac, pct))
            pv_total += pv
            ev_total += ev
        if missing_progress:
            warnings.append(f"{len(missing_progress)} faaliyet için {as_of} tarihine kadar ilerleme girilmemiş (EV 0 alındı): "
                            + ", ".join(missing_progress[:5]))
        lines = self.ctl._lines(project_id, as_of)
        ac_total = sum((ln.amount for ln in lines), ZERO)
        allocated = sum((sum(d.values(), ZERO) for d in alloc.values()), ZERO)
        unalloc = ac_total - allocated
        if unalloc != 0:
            warnings.append(f"Faaliyete tahsis edilmemiş gerçekleşen maliyet var: {unalloc} (proje AC'sine dahildir).")
        n_unmatched = len([r for r in self.ctl.scope_report(project_id) if r["allocated_pct"] == 0])
        if n_unmatched and any(self.ctl.activity_scope(a.id) for a in live):
            warnings.append(f"{n_unmatched} keşif kalemi hiçbir faaliyete bağlı değil.")
        if base is not None:
            bac, src = sum((i["planned_cost"] for i in items), ZERO), f"Baz plan rev.{base.revision_no} ({base.name})"
        elif items:
            bac, src = sum((i["planned_cost"] for i in items), ZERO), "Güncel plan (baz plan yok)"
        else:
            budget = sum(self.ctl.costs.budgets(project_id).values(), ZERO)
            bac, src = (budget, "Maliyet bütçesi") if budget > 0 else (None, "Plan ve bütçe yok")
            warnings.append("Henüz plan yok: göstergeler hesaplanamıyor.")
        series = self._series(items, lines)
        return Evm(project_id, as_of, base, bac, src, quantize(pv_total, 2), quantize(ev_total, 2), ac_total, unalloc, rows,
                   warnings, series)

    @staticmethod
    def _series(items: list[dict], lines) -> list[tuple[str, Decimal, Decimal]]:
        plan: dict[str, Decimal] = {}
        for it in items:
            for k, v in it["monthly"].items():
                plan[k] = plan.get(k, ZERO) + v
        act: dict[str, Decimal] = {}
        for ln in lines:
            act[ln.date[:7]] = act.get(ln.date[:7], ZERO) + ln.amount
        return [(k, plan.get(k, ZERO), act.get(k, ZERO)) for k in sorted(set(plan) | set(act))]
