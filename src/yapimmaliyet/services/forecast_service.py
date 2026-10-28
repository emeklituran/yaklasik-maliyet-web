"""Proje bitiş maliyeti (EAC) ve beklenen kâr.

Formüller (docs/TEKNIK_KARARLAR.md K11–K12; şartname §12):
  ETC = açık taahhüt + taahhütsüz kalan iş tahmini (kullanıcı onaylı, sürümlü, gerekçeli)
  EAC = AC + ETC            ·  Bütçe sapması = EAC − BAC
  Gelir = idari sözleşmelerin güncel bedeli (YALNIZ onaylı değişiklikler; onay bekleyenler ayrıca "potansiyel")
  Beklenen kâr = Gelir − EAC  ·  Marj = kâr / gelir   (hepsi KDV hariç)
Taahhütsüz ETC girilmemişse EAC "eksik" işaretlenir (alt sınır: AC + açık taahhüt) — 0 varsayılıp tam sonuçmuş gibi sunulmaz.
Her bileşen kaynağına geri izlenir (`trace`). Aynı veriyle her çalıştırmada aynı sonuç üretilir.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.commitment_service import CommitmentService, CommitmentSummary
from yapimmaliyet.services.contract_service import ContractService
from yapimmaliyet.services.evm_service import EvmService
from yapimmaliyet.services.validators import clean, require, validate_iso_date

ZERO = Decimal(0)
SCENARIOS = {"IYIMSER": "İyimser", "BAZ": "Baz", "KOTUMSER": "Kötümser"}


@dataclass
class Forecast:
    project_id: int
    as_of: str
    scenario: str
    bac: Decimal | None
    bac_source: str
    ac: Decimal
    open_commitment: Decimal
    uncommitted_etc: Decimal | None
    etc_version: int | None
    revenue: Decimal
    pending_revenue: Decimal
    commitments: CommitmentSummary
    trace: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.uncommitted_etc is not None

    @property
    def etc(self) -> Decimal:
        return self.open_commitment + (self.uncommitted_etc or ZERO)

    @property
    def eac(self) -> Decimal:
        return self.ac + self.etc

    @property
    def profit(self) -> Decimal:
        return self.revenue - self.eac

    @property
    def margin(self) -> Decimal | None:
        return quantize(self.profit * 100 / self.revenue, 2) if self.revenue > 0 else None

    @property
    def variance_to_bac(self) -> Decimal | None:
        return None if self.bac is None else self.eac - self.bac


class ForecastService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.commit = CommitmentService(conn)
        self.evm = EvmService(conn)
        self.contracts = ContractService(conn)

    # --- ETC (taahhütsüz kalan iş) sürümleri ------------------------------------
    def set_etc(self, project_id: int, scenario: str, amount, reason: str, confirmed_by: str, as_of: str | None = None,
                excludes_open_commitment: bool = False) -> int:
        if scenario not in SCENARIOS:
            raise ValidationError("Geçersiz senaryo.")
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError("Proje bulunamadı.")
        if not excludes_open_commitment:
            raise ValidationError("Taahhütsüz kalan iş tahmini, açık taahhüdü İÇERMEMELİDİR (çakışma = çift sayım); bunu onaylayın.")
        try:
            amt = to_decimal(amount)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Tahmin tutarı geçerli bir sayı değil.") from exc
        if amt < 0:
            raise ValidationError("Taahhütsüz kalan iş tahmini negatif olamaz.")
        d = validate_iso_date(as_of or date.today().isoformat(), "Tahmin tarihi", required=True)
        with transaction(self.conn):
            ver = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM forecast_etc WHERE project_id = ? AND scenario = ?",
                                    (project_id, scenario)).fetchone()[0]
            eid = self.conn.execute(
                "INSERT INTO forecast_etc(project_id, scenario, version, amount, reason, as_of, confirmed_by,"
                " excludes_open_commitment) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (project_id, scenario, ver, to_db_text(quantize(amt, 2), 2), require(reason, "Gerekçe"), d,
                 require(confirmed_by, "Onaylayan"))).lastrowid
            audit.log(self.conn, "forecast_etc", eid, "EKLE", new={"project": project_id, "scenario": scenario, "version": ver,
                                                                   "amount": str(amt)})
        return eid

    def etc_history(self, project_id: int, scenario: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM forecast_etc WHERE project_id = ?" + (" AND scenario = ?" if scenario else "")
        return self.conn.execute(sql + " ORDER BY scenario, version DESC", (project_id, scenario) if scenario else (project_id,)).fetchall()

    def current_etc(self, project_id: int, scenario: str, as_of: str | None = None) -> sqlite3.Row | None:
        """Tarihte geçerli son sürüm (as_of ≤ tarih)."""
        return self.conn.execute("SELECT * FROM forecast_etc WHERE project_id = ? AND scenario = ? AND as_of <= ?"
                                 " ORDER BY version DESC LIMIT 1", (project_id, scenario, as_of or "9999-12-31")).fetchone()

    # --- hesap -------------------------------------------------------------------
    def revenue(self, project_id: int) -> tuple[Decimal, Decimal, list[str]]:
        total, pending, trace = ZERO, ZERO, []
        for r in self.conn.execute("SELECT id, title, contract_amount FROM pp_contracts WHERE kind = 'IDARI' AND project_id = ? ORDER BY id",
                                   (project_id,)):
            s = self.contracts.summary(r["id"])
            total += s.current_amount
            pend = sum((c.amount_delta for c in s.pending if c.amount_delta), ZERO)
            pending += pend
            trace.append(f"Gelir — {r['title']}: asıl {from_db_text(r['contract_amount'])} + onaylı değişiklik {s.amount_delta} = {s.current_amount}"
                         + (f" (onay bekleyen {pend} dahil değil)" if pend else ""))
        return total, pending, trace

    def evaluate(self, project_id: int, as_of: str | None = None, scenario: str = "BAZ") -> Forecast:
        if scenario not in SCENARIOS:
            raise ValidationError("Geçersiz senaryo.")
        as_of = as_of or date.today().isoformat()
        evm = self.evm.evaluate(project_id, as_of)
        cs = self.commit.summary(project_id, as_of)
        revenue, pending, rtrace = self.revenue(project_id)
        etc = self.current_etc(project_id, scenario, as_of)
        trace = [f"AC: {as_of} tarihine kadar gider kaydı + teslim + taşeron hakedişi = {evm.ac}",
                 f"Açık taahhüt: {len(cs.rows)} taahhüt (sipariş + taşeron sözleşmesi) → açık {cs.open}"] + rtrace
        warnings = list(evm.warnings)
        if etc is None:
            trace.append(f"Taahhütsüz kalan iş tahmini ({SCENARIOS[scenario]}): girilmemiş")
            warnings.append(f"{SCENARIOS[scenario]} senaryosu için taahhütsüz kalan iş tahmini girilmemiş: EAC alt sınırdır (AC + açık taahhüt).")
            amount, ver = None, None
        else:
            amount, ver = from_db_text(etc["amount"]), etc["version"]
            trace.append(f"Taahhütsüz kalan iş ({SCENARIOS[scenario]}) sürüm {ver}: {amount} — {etc['reason']} "
                         f"({etc['confirmed_by']}, {etc['as_of']})")
        if cs.exceptions:
            warnings.append(f"{len(cs.exceptions)} taahhütte fazla gerçekleşme (istisna): " + ", ".join(c.ref for c in cs.exceptions[:4]))
        if pending:
            warnings.append(f"Onay bekleyen sözleşme değişikliği {pending}: gelire ve taahhüde DAHİL DEĞİL.")
        if revenue == 0:
            warnings.append("İdari sözleşme geliri yok: kâr hesaplanamıyor (gelir 0 kabul edildi).")
        return Forecast(project_id, as_of, scenario, evm.bac, evm.bac_source, evm.ac, cs.open, amount, ver, revenue, pending, cs,
                        trace, warnings)

    def scenarios(self, project_id: int, as_of: str | None = None) -> list[Forecast]:
        return [self.evaluate(project_id, as_of, s) for s in SCENARIOS]

    def save_snapshot(self, project_id: int, scenario: str, as_of: str | None = None, created_by: str = "") -> int:
        f = self.evaluate(project_id, as_of, scenario)
        with transaction(self.conn):
            sid = self.conn.execute(
                "INSERT INTO forecast_snapshots(project_id, as_of, scenario, bac, ac, open_commitment, uncommitted_etc, etc, eac,"
                " revenue, profit, complete, assumptions_json, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, f.as_of, scenario, None if f.bac is None else to_db_text(f.bac, 2), to_db_text(f.ac, 2),
                 to_db_text(f.open_commitment, 2), None if f.uncommitted_etc is None else to_db_text(f.uncommitted_etc, 2),
                 to_db_text(f.etc, 2), to_db_text(f.eac, 2), to_db_text(f.revenue, 2), to_db_text(f.profit, 2), int(f.complete),
                 json.dumps({"trace": f.trace, "warnings": f.warnings, "etc_version": f.etc_version}, ensure_ascii=False),
                 clean(created_by) or "")).lastrowid
            audit.log(self.conn, "forecast_snapshot", sid, "EKLE", new={"project": project_id, "scenario": scenario, "eac": str(f.eac)})
        return sid

    def snapshots(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM forecast_snapshots WHERE project_id = ? ORDER BY id DESC", (project_id,)).fetchall()
