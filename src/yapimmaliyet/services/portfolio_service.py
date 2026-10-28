"""Gelişmiş yönetim panosu (portföy) verisi (şartname N4): proje portföyü, risk göstergeleri, EAC trendi, gecikme ve taahhüt.

Yeni hesap kuralı üretmez: bütün sayılar EVM, tahmin, taahhüt, faaliyet planı ve doküman servislerinden gelir; böylece pano ile
raporlar aynı sayıyı gösterir. Risk göstergesi yalnız ölçülebilen veriden doğar (ölçülemeyen gösterge "Veri yok"tur, bayrak
üretmez). Bayrak koşulları tanımsal eşiklerdir (CPI/SPI < 1, EAC > BAC, kâr < 0); keyfî puan ya da ağırlık yoktur.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.services.activity_plan_service import ActivityPlanService
from yapimmaliyet.services.commitment_service import CommitmentService
from yapimmaliyet.services.evm_service import EvmService
from yapimmaliyet.services.forecast_service import ForecastService

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass
class Delay:
    activity_id: int
    name: str
    planned_end: str
    days: int
    progress: Decimal


@dataclass
class ProjectRow:
    project_id: int
    name: str
    stage: str
    bac: Decimal | None
    ac: Decimal
    eac: Decimal
    eac_complete: bool
    revenue: Decimal
    profit: Decimal
    cpi: Decimal | None
    spi: Decimal | None
    open_commitment: Decimal
    delayed: int
    pending_docs: int
    flags: list[str] = field(default_factory=list)


class PortfolioService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.evm = EvmService(conn)
        self.fc = ForecastService(conn)
        self.plan = ActivityPlanService(conn)

    # --- proje düzeyi ------------------------------------------------------------
    def delays(self, project_id: int, as_of: str | None = None) -> list[Delay]:
        as_of = as_of or date.today().isoformat()
        today = date.fromisoformat(as_of)
        out = []
        for a in self.plan.activities(project_id):
            if a.planned_end < as_of and a.progress < 100:
                out.append(Delay(a.id, a.name, a.planned_end, (today - date.fromisoformat(a.planned_end)).days, a.progress))
        return sorted(out, key=lambda d: -d.days)

    def eac_trend(self, project_id: int, scenario: str = "BAZ") -> list[tuple[str, Decimal, Decimal, bool]]:
        """Kayıtlı tahmin anlık görüntüleri: (tarih, EAC, beklenen kâr, tam mı). Anlık görüntü yoksa liste boştur."""
        rows = self.conn.execute("SELECT as_of, eac, profit, complete FROM forecast_snapshots WHERE project_id = ? AND scenario = ?"
                                 " ORDER BY as_of, id", (project_id, scenario)).fetchall()
        return [(r["as_of"], from_db_text(r["eac"]), from_db_text(r["profit"]), bool(r["complete"])) for r in rows]

    def commitments(self, project_id: int, as_of: str | None = None):
        return CommitmentService(self.conn).summary(project_id, as_of).rows

    def row(self, project_id: int, as_of: str | None = None) -> ProjectRow:
        as_of = as_of or date.today().isoformat()
        p = self.conn.execute("SELECT name, work_stage, tender_status FROM projects WHERE id = ?", (project_id,)).fetchone()
        evm = self.evm.evaluate(project_id, as_of)
        fc = self.fc.evaluate(project_id, as_of)
        delayed = self.delays(project_id, as_of)
        docs = self.conn.execute("SELECT COUNT(*) FROM doc_documents WHERE project_id = ? AND status = 'INCELEMEDE'", (project_id,)).fetchone()[0]
        flags = []
        if evm.cpi is not None and evm.cpi < ONE:
            flags.append(f"Maliyet aşımı (CPI {evm.cpi})")
        if evm.spi is not None and evm.spi < ONE:
            flags.append(f"Takvim gerisi (SPI {evm.spi})")
        if fc.bac is not None and fc.eac > fc.bac:
            flags.append("EAC bütçeyi aşıyor")
        if fc.revenue > 0 and fc.profit < 0:
            flags.append("Beklenen zarar")
        if not fc.complete:
            flags.append("Tahmin eksik (kalan iş girilmemiş)")
        if fc.commitments.exceptions:
            flags.append(f"{len(fc.commitments.exceptions)} taahhütte fazla gerçekleşme")
        if delayed:
            flags.append(f"{len(delayed)} faaliyet gecikmiş")
        if docs:
            flags.append(f"{docs} doküman onay bekliyor")
        return ProjectRow(project_id, p["name"], p["work_stage"] or p["tender_status"] or "", evm.bac, evm.ac, fc.eac, fc.complete,
                          fc.revenue, fc.profit, evm.cpi, evm.spi, fc.open_commitment, len(delayed), docs, flags)

    # --- portföy --------------------------------------------------------------------
    def portfolio(self, as_of: str | None = None) -> list[ProjectRow]:
        """İş verisi olan projeler (idari sözleşmesi ya da faaliyet planı olanlar); risk bayrağı çok olan önce."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM projects WHERE id IN (SELECT project_id FROM pp_contracts WHERE kind = 'IDARI' AND project_id IS NOT NULL)"
            " OR id IN (SELECT project_id FROM ws_activities) ORDER BY name")]
        rows = [self.row(i, as_of) for i in ids]
        return sorted(rows, key=lambda r: (-len(r.flags), r.name))

    @staticmethod
    def totals(rows: list[ProjectRow]) -> dict[str, Decimal]:
        return {"ac": sum((r.ac for r in rows), ZERO), "eac": sum((r.eac for r in rows), ZERO),
                "revenue": sum((r.revenue for r in rows), ZERO), "profit": sum((r.profit for r in rows), ZERO),
                "open_commitment": sum((r.open_commitment for r in rows), ZERO)}
