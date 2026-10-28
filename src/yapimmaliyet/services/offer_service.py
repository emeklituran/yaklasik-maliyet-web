"""Firma maliyeti çalışmaları ve teklif senaryoları; karşılaştırmanın kesinleştirilmesi."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.migrator import current_version
from yapimmaliyet.db.repositories import audit, calculations, cost_sheets, estimate_items, projects
from yapimmaliyet.db.repositories.cost_sheets import Sheet
from yapimmaliyet.domain.firm_cost import (
    COMPONENTS,
    CostLineInput,
    CostResult,
    IndirectCost,
    IndirectMethod,
    OfferResult,
    Scenario,
    compute_cost,
    compute_offer,
)
from yapimmaliyet.domain.money import decimal_places, to_decimal
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.calc_service import CalcService
from yapimmaliyet.services.validators import clean, require


@dataclass(frozen=True)
class OfferAnalysis:
    cost: CostResult
    offers: tuple[OfferResult, ...]
    reference_total: Decimal | None
    reference_label: str


def _rate(value, label: str, lo: Decimal = Decimal(0), hi: Decimal = Decimal(1)) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçersiz.") from exc
    if not lo <= d <= hi:
        raise ValidationError(f"{label} %{lo * 100:.0f} ile %{hi * 100:.0f} arasında olmalıdır.")
    return d


class OfferService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- çalışmalar --------------------------------------------------------
    def sheets(self, estimate_id: int) -> list[Sheet]:
        return cost_sheets.list_sheets(self.conn, estimate_id)

    def _sheet(self, sheet_id: int) -> Sheet:
        s = cost_sheets.get_sheet(self.conn, sheet_id)
        if s is None:
            raise NotFoundError("Maliyet çalışması bulunamadı.")
        return s

    def create_sheet(self, estimate_id: int, name: str) -> int:
        if projects.get_estimate(self.conn, estimate_id) is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        name = require(name, "Çalışma adı")
        if any(s.name == name for s in self.sheets(estimate_id)):
            raise ConflictError("Bu keşifte aynı adlı bir maliyet çalışması var.")
        with transaction(self.conn):
            sid = cost_sheets.insert_sheet(self.conn, estimate_id, name)
            audit.log(self.conn, "cost_sheet", sid, "EKLE", new={"name": name})
        return sid

    def copy_sheet(self, sheet_id: int, name: str) -> int:
        """Alternatif varsayımlar için çalışmanın kopyası (bileşenler, dolaylı giderler, senaryolar)."""
        s = self._sheet(sheet_id)
        name = require(name, "Çalışma adı")
        if any(x.name == name for x in self.sheets(s.estimate_id)):
            raise ConflictError("Bu keşifte aynı adlı bir maliyet çalışması var.")
        with transaction(self.conn):
            new_id = cost_sheets.copy_sheet(self.conn, sheet_id, name)
            audit.log(self.conn, "cost_sheet", new_id, "EKLE", new={"name": name, "copied_from": sheet_id},
                      note=f"'{s.name}' çalışmasından kopyalandı")
        return new_id

    def delete_sheet(self, sheet_id: int) -> None:
        s = self._sheet(sheet_id)
        with transaction(self.conn):
            cost_sheets.delete_sheet(self.conn, sheet_id)
            audit.log(self.conn, "cost_sheet", sheet_id, "SIL", old={"name": s.name})

    def set_component(self, sheet_id: int, item_id: int, key: str, value: Decimal) -> None:
        s = self._sheet(sheet_id)
        if key not in COMPONENTS:
            raise ValidationError("Geçersiz maliyet bileşeni.")
        try:
            v = to_decimal(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Geçersiz tutar: {value}") from exc
        if v < 0 or decimal_places(v) > 4:
            raise ValidationError("Maliyet negatif olamaz ve en fazla 4 ondalık basamak içerebilir.")
        item = estimate_items.get_item(self.conn, item_id)
        if item is None or item.estimate_id != s.estimate_id:
            raise NotFoundError("Kalem bu çalışmanın keşfine ait değil.")
        with transaction(self.conn):
            cost_sheets.set_component(self.conn, sheet_id, item_id, key, v)

    def indirects(self, sheet_id: int) -> list[tuple[int, IndirectCost]]:
        return cost_sheets.indirects(self.conn, sheet_id)

    def add_indirect(self, sheet_id: int, name: str, method: IndirectMethod, value: Decimal,
                     note: str | None = None) -> int:
        self._sheet(sheet_id)
        name = require(name, "Gider adı")
        value = (_rate(value, "Oran") if method is IndirectMethod.ORAN
                 else _rate(value, "Tutar", hi=Decimal("1e15")))
        with transaction(self.conn):
            iid = cost_sheets.insert_indirect(self.conn, sheet_id, IndirectCost(name, method, value, clean(note)))
            audit.log(self.conn, "cost_sheet", sheet_id, "GUNCELLE", new={"indirect": name, "value": value})
        return iid

    def remove_indirect(self, indirect_id: int) -> None:
        with transaction(self.conn):
            cost_sheets.delete_indirect(self.conn, indirect_id)

    def scenarios(self, sheet_id: int) -> list[tuple[int, Scenario]]:
        return cost_sheets.scenarios(self.conn, sheet_id)

    def add_scenario(self, sheet_id: int, name: str, profit_rate: Decimal, risk_rate: Decimal = Decimal(0),
                     notes: str | None = None) -> int:
        self._sheet(sheet_id)
        name = require(name, "Senaryo adı")
        if any(s.name == name for _, s in self.scenarios(sheet_id)):
            raise ConflictError("Aynı adlı senaryo var.")
        s = Scenario(name, _rate(profit_rate, "Kâr oranı", Decimal("-0.5"), Decimal(2)),
                     _rate(risk_rate, "Risk oranı", Decimal(0), Decimal(1)), clean(notes))
        with transaction(self.conn):
            sid = cost_sheets.insert_scenario(self.conn, sheet_id, s)
            audit.log(self.conn, "cost_sheet", sheet_id, "GUNCELLE", new={"scenario": name})
        return sid

    def remove_scenario(self, scenario_id: int) -> None:
        with transaction(self.conn):
            cost_sheets.delete_scenario(self.conn, scenario_id)

    # --- hesap -------------------------------------------------------------
    def cost_inputs(self, sheet_id: int) -> list[CostLineInput]:
        s = self._sheet(sheet_id)
        comps = cost_sheets.components(self.conn, sheet_id)
        return [CostLineInput(it.id, it.line_no, it.work_group, it.code or it.matched_code,
                              it.description or it.matched_description, it.unit or it.matched_unit, it.quantity,
                              comps.get(it.id, {}))
                for it in estimate_items.list_items(self.conn, s.estimate_id)]

    def reference(self, estimate_id: int) -> tuple[Decimal | None, str]:
        """Karşılaştırma tabanı: keşfin en son kesinleşmiş tahmini yaklaşık maliyeti (yoksa referans fiyatı)."""
        calc = CalcService(self.conn)
        for kind in ("YAKLASIK", "REFERANS"):
            for snap in calc.snapshots(estimate_id):
                if snap.kind == kind:
                    p = calc.snapshot_payload(snap.id)
                    return Decimal(p["totals"]["total_excl_vat"]), f"{p['kind_label']} — kayıt no {snap.id}"
        return None, "Karşılaştırma için kesinleşmiş yaklaşık maliyet/referans hesabı yok"

    def analyse(self, sheet_id: int) -> OfferAnalysis:
        s = self._sheet(sheet_id)
        try:
            cost = compute_cost(self.cost_inputs(sheet_id), [i for _, i in self.indirects(sheet_id)])
            ref, label = self.reference(s.estimate_id)
            offers = tuple(compute_offer(cost, sc, ref) for _, sc in self.scenarios(sheet_id))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return OfferAnalysis(cost, offers, ref, label)

    def finalize(self, sheet_id: int, name: str | None = None) -> int:
        s = self._sheet(sheet_id)
        a = self.analyse(sheet_id)
        if not a.cost.lines:
            raise ConflictError("Keşifte kalem yok.")
        if not a.offers:
            raise ConflictError("En az bir teklif senaryosu tanımlayın.")
        profile = next((p for p in calculations.list_profiles(self.conn, "FIRMA")), None)
        if profile is None:
            raise NotFoundError("Firma profili bulunamadı.")
        est = projects.get_estimate(self.conn, s.estimate_id)
        project = projects.get_project(self.conn, est.project_id)
        payload = offer_payload(a, s, est, project, name)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with transaction(self.conn):
            calc_id = calculations.insert_calculation(self.conn, s.estimate_id, profile.id, "TEKLIF", name,
                                                      est.source_priority)
            snap = calculations.insert_snapshot(self.conn, calc_id, __version__, current_version(self.conn), text,
                                                hashlib.sha256(text.encode("utf-8")).hexdigest())
            audit.log(self.conn, "calculation", calc_id, "EKLE", note="Teklif karşılaştırması kesinleştirildi")
        return snap


def _s(v) -> str | None:
    return None if v is None else str(v)


def offer_payload(a: OfferAnalysis, sheet: Sheet, est, project, name: str | None, draft: bool = False) -> dict:
    return {
        "schema": 1, "draft": draft, "created_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": __version__, "name": name, "kind": "TEKLIF", "kind_label": "Firma maliyeti ve teklif senaryoları",
        "project": {"id": project.id, "name": project.name, "ikn": project.ikn, "authority": project.authority_name,
                    "offer_type": project.offer_type},
        "estimate": {"id": est.id, "name": est.name, "revision": est.revision},
        "sheet": {"id": sheet.id, "name": sheet.name},
        "cost_lines": [{
            "line_no": c.input.line_no, "work_group": c.input.work_group, "code": c.input.code,
            "description": c.input.description, "unit": c.input.unit, "quantity": _s(c.input.quantity),
            "components": {k: _s(c.input.components.get(k, Decimal(0))) for k in COMPONENTS},
            "direct_unit": _s(c.direct_unit), "direct_amount": _s(c.direct_amount),
            "indirect_amount": _s(c.indirect_amount), "total_cost": _s(c.total_cost)} for c in a.cost.lines],
        "indirects": [{"name": i.name, "method": i.method.value, "value": _s(i.value), "amount": _s(amt),
                       "note": i.note} for i, amt in a.cost.indirect_items],
        "cost_totals": {"direct": _s(a.cost.direct_total), "indirect": _s(a.cost.indirect_total),
                        "total": _s(a.cost.total_cost)},
        "reference": {"total": _s(a.reference_total), "label": a.reference_label},
        "offers": [{
            "name": o.scenario.name, "profit_rate": _s(o.scenario.profit_rate), "risk_rate": _s(o.scenario.risk_rate),
            "notes": o.scenario.notes, "offer_total": _s(o.offer_total), "profit_amount": _s(o.profit_amount),
            "margin": _s(o.margin), "ratio_to_reference": _s(o.ratio_to_reference),
            "lines": [{"line_no": ln.cost.input.line_no, "unit_price": _s(ln.unit_price), "amount": _s(ln.amount)}
                      for ln in o.lines]} for o in a.offers],
        "notes": a.cost.warnings + [
            "Kâr ve risk oranları firma tercihidir; mevzuat kuralı değildir.",
            "Teklif tutarları KDV hariçtir. Aşırı düşük teklif ve sınır değer değerlendirmesi bu hesaba dahil değildir.",
        ],
    }
