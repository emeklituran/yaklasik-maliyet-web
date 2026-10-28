"""Proje kontrolü veri katmanı: WBS, baz plan sürümleri, ilerleme snapshot'ları, kapsam (keşif) ve maliyet tahsisi.

İlkeler (docs/TEKNIK_KARARLAR.md):
- Baz plan, kilitlenen değişmez anlık görüntüdür; revizyon yeni sürümdür (gerekçe, tarih, önceki sürüm). Güncel plan canlı faaliyetlerdir.
- İlerleme as-of tarihli snapshot'tır; geçmiş tarihli rapor yeniden üretilebilir.
- Kapsam tahsisi (faaliyet ↔ keşif kalemi) ve maliyet tahsisi (kaynak olay ↔ faaliyet) ORANLIDIR; her kalem/kaynak için Σ oran ≤ %100.
  Maliyet tahsisinde tutar kaynaktan okunur (kopya yok): kaynak toplamı tahsisle değişmez, kalan "tahsis edilmemiş" görünür.
- Projeler arası sızma yok: faaliyet, keşif kalemi ve maliyet kaynağı aynı projeye ait olmalıdır.
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
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.activity_plan_service import Activity, ActivityPlanService
from yapimmaliyet.services.cost_tracking_service import ActualLine, CostTrackingService
from yapimmaliyet.services.validators import clean, require, validate_iso_date

ZERO = Decimal(0)
HUNDRED = Decimal(100)
SOURCE_TYPES = {"GIDER": "Gider kaydı", "TESLIM": "Satın alma teslimi", "TASERON_HAKEDIS": "Taşeron hakedişi"}
PROGRESS_METHODS = {"MANUEL_YUZDE": "Manuel fiziksel yüzde", "MIKTAR": "Miktar esaslı"}


def _ratio(value, label: str = "Oran") -> Decimal:
    try:
        r = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçerli bir sayı değil.") from exc
    if not ZERO < r <= HUNDRED:
        raise ValidationError(f"{label} 0'dan büyük, en çok 100 olmalıdır (yüzde).")
    return quantize(r, 2)


def split_amount(amount: Decimal, ratios: list[Decimal]) -> list[Decimal]:
    """Tutarı oranlara böler (kuruş). Oranlar toplamı %100 ise son pay kalanı alır → toplam kaynağa tam eşit."""
    parts = [quantize(amount * r / HUNDRED, 2) for r in ratios]
    if ratios and sum(ratios, ZERO) == HUNDRED:
        parts[-1] = amount - sum(parts[:-1], ZERO)
    return parts


@dataclass(frozen=True)
class WbsNode:
    code: str
    level: int
    title: str
    planned_cost: Decimal
    activity_id: int | None
    has_children: bool


@dataclass(frozen=True)
class Baseline:
    id: int
    project_id: int
    revision_no: int
    name: str
    reason: str
    locked: bool
    approved_at: str | None
    approved_by: str | None
    supersedes_id: int | None
    item_count: int = 0
    total: Decimal = ZERO


@dataclass(frozen=True)
class AllocRow:
    activity_id: int
    ratio: Decimal
    amount: Decimal
    implicit: bool = False


@dataclass
class CostSource:
    source_type: str
    source_id: int
    date: str
    description: str
    amount: Decimal
    allocations: list[AllocRow] = field(default_factory=list)

    @property
    def allocated(self) -> Decimal:
        return sum((a.amount for a in self.allocations), ZERO)

    @property
    def unallocated(self) -> Decimal:
        return self.amount - self.allocated


class ScheduleControlService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.plan = ActivityPlanService(conn)
        self.costs = CostTrackingService(conn)

    # --- yardımcı --------------------------------------------------------------
    def _activity(self, activity_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM ws_activities WHERE id = ?", (activity_id,)).fetchone()
        if r is None:
            raise NotFoundError("Faaliyet bulunamadı.")
        return r

    # --- WBS -------------------------------------------------------------------
    def wbs_tree(self, project_id: int) -> list[WbsNode]:
        acts = [a for a in self.plan.activities(project_id) if a.wbs_code]
        codes = {a.wbs_code for a in acts}
        nodes: set[str] = set()
        for c in codes:
            parts = c.split(".")
            nodes.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        by_code = {a.wbs_code: a for a in acts}
        out = []
        for code in sorted(nodes, key=lambda c: [int(x) if x.isdigit() else x for x in c.split(".")]):
            kids = [n for n in nodes if n.startswith(code + ".")]
            leaves = [a for a in acts if (a.wbs_code == code or (a.wbs_code or "").startswith(code + "."))
                      and not any(o.startswith(a.wbs_code + ".") for o in nodes)]
            act = by_code.get(code)
            out.append(WbsNode(code, code.count(".") + 1, act.name if act else f"WBS {code}",
                               sum((a.planned_cost for a in leaves), ZERO), act.id if act else None, bool(kids)))
        return out

    # --- baz plan --------------------------------------------------------------
    def baselines(self, project_id: int) -> list[Baseline]:
        out = []
        for r in self.conn.execute("SELECT * FROM sched_baselines WHERE project_id = ? ORDER BY revision_no", (project_id,)):
            items = self.items(r["id"])
            out.append(Baseline(r["id"], r["project_id"], r["revision_no"], r["name"], r["reason"], bool(r["locked"]),
                                r["approved_at"], r["approved_by"], r["supersedes_id"], len(items),
                                sum((i["planned_cost"] for i in items), ZERO)))
        return out

    def baseline(self, baseline_id: int) -> Baseline:
        r = self.conn.execute("SELECT project_id FROM sched_baselines WHERE id = ?", (baseline_id,)).fetchone()
        if r is None:
            raise NotFoundError("Baz plan bulunamadı.")
        return next(b for b in self.baselines(r["project_id"]) if b.id == baseline_id)

    def latest_locked(self, project_id: int) -> Baseline | None:
        locked = [b for b in self.baselines(project_id) if b.locked]
        return locked[-1] if locked else None

    def items(self, baseline_id: int) -> list[dict]:
        return [{"id": r["id"], "activity_id": r["activity_id"], "external_code": r["external_code"], "wbs_code": r["wbs_code"],
                 "title": r["title"], "discipline": r["discipline"], "planned_start": r["planned_start"],
                 "planned_end": r["planned_end"], "planned_cost": from_db_text(r["planned_cost"]),
                 "monthly": {k: Decimal(v) for k, v in json.loads(r["monthly_json"] or "{}").items()}}
                for r in self.conn.execute("SELECT * FROM sched_baseline_items WHERE baseline_id = ? ORDER BY id", (baseline_id,))]

    def create_baseline(self, project_id: int, name: str, reason: str = "") -> int:
        """Güncel planın taslak anlık görüntüsünü alır; `lock_baseline` ile onaylanıp kilitlenir."""
        acts = self.plan.activities(project_id)
        if not acts:
            raise ValidationError("Baz plan için projede en az bir faaliyet olmalıdır.")
        if any(not b.locked for b in self.baselines(project_id)):
            raise ConflictError("Kilitlenmemiş bir baz plan taslağı var; önce onaylayın ya da silin.")
        with transaction(self.conn):
            no = self.conn.execute("SELECT COALESCE(MAX(revision_no), 0) + 1 FROM sched_baselines WHERE project_id = ?",
                                   (project_id,)).fetchone()[0]
            bid = self.conn.execute("INSERT INTO sched_baselines(project_id, revision_no, name, reason) VALUES (?, ?, ?, ?)",
                                    (project_id, no, require(name, "Baz plan adı"), clean(reason) or "")).lastrowid
            for a in acts:
                months = {k: str(v) for k, v in self.plan.effective_months(a).items()}
                self.conn.execute(
                    "INSERT INTO sched_baseline_items(baseline_id, activity_id, external_code, wbs_code, title, discipline,"
                    " planned_start, planned_end, planned_cost, monthly_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (bid, a.id, a.external_code, a.wbs_code, a.name, a.discipline, a.planned_start, a.planned_end,
                     to_db_text(a.planned_cost, 2), json.dumps(months)))
            audit.log(self.conn, "sched_baseline", bid, "EKLE", new={"project": project_id, "revision": no, "items": len(acts)})
        return bid

    def lock_baseline(self, baseline_id: int, approved_by: str) -> None:
        b = self.baseline(baseline_id)
        if b.locked:
            raise ConflictError("Baz plan zaten kilitli.")
        if b.revision_no > 1 and not b.reason:
            raise ValidationError("Revizyon sürümünde gerekçe zorunludur.")
        prev = self.latest_locked(b.project_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE sched_baselines SET locked = 1, approved_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'),"
                              " approved_by = ?, supersedes_id = ? WHERE id = ?",
                              (require(approved_by, "Onaylayan"), prev.id if prev else None, baseline_id))
            audit.log(self.conn, "sched_baseline", baseline_id, "GUNCELLE", new={"locked": True, "by": approved_by},
                      note="Baz plan kilitlendi")

    def set_reason(self, baseline_id: int, reason: str) -> None:
        if self.baseline(baseline_id).locked:
            raise ConflictError("Kilitli baz planın gerekçesi değiştirilemez.")
        with transaction(self.conn):
            self.conn.execute("UPDATE sched_baselines SET reason = ? WHERE id = ?", (clean(reason) or "", baseline_id))

    def discard_draft(self, baseline_id: int) -> None:
        if self.baseline(baseline_id).locked:
            raise ConflictError("Kilitli baz plan silinemez; yeni revizyon açın.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM sched_baselines WHERE id = ?", (baseline_id,))
            audit.log(self.conn, "sched_baseline", baseline_id, "SIL")

    def compare(self, a_id: int, b_id: int) -> list[dict]:
        """İki baz plan sürümü arasındaki tarih ve maliyet farkları (faaliyet eşleşmesi kod, yoksa ad ile)."""
        def key(i):
            return i["external_code"] or i["title"].casefold()

        a, b = {key(i): i for i in self.items(a_id)}, {key(i): i for i in self.items(b_id)}
        rows = []
        for k in list(a) + [k for k in b if k not in a]:
            x, y = a.get(k), b.get(k)
            rows.append({"key": k, "title": (y or x)["title"],
                         "durum": "kaldırıldı" if y is None else "eklendi" if x is None else "değişti" if (
                             x["planned_start"], x["planned_end"], x["planned_cost"]) != (
                             y["planned_start"], y["planned_end"], y["planned_cost"]) else "aynı",
                         "start_a": x and x["planned_start"], "start_b": y and y["planned_start"],
                         "end_a": x and x["planned_end"], "end_b": y and y["planned_end"],
                         "cost_a": x["planned_cost"] if x else ZERO, "cost_b": y["planned_cost"] if y else ZERO,
                         "cost_diff": (y["planned_cost"] if y else ZERO) - (x["planned_cost"] if x else ZERO),
                         "days_shift": ((date.fromisoformat(y["planned_end"]) - date.fromisoformat(x["planned_end"])).days
                                        if x and y else None)})
        return rows

    # --- ilerleme snapshot ------------------------------------------------------
    def record_progress(self, activity_id: int, as_of: str, pct, method: str = "MANUEL_YUZDE", note: str = "",
                        approved_by: str = "") -> None:
        self._activity(activity_id)
        if method not in PROGRESS_METHODS:
            raise ValidationError("Geçersiz ilerleme yöntemi.")
        d = validate_iso_date(as_of, "Ölçüm tarihi", required=True)
        try:
            p = to_decimal(pct)
        except (TypeError, ValueError) as exc:
            raise ValidationError("İlerleme yüzdesi geçerli bir sayı değil.") from exc
        if not ZERO <= p <= HUNDRED:
            raise ValidationError("İlerleme yüzdesi 0 ile 100 arasında olmalıdır.")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO activity_progress_snapshots(activity_id, as_of, physical_pct, method, note, approved_by)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(activity_id, as_of) DO UPDATE SET physical_pct = excluded.physical_pct,"
                " method = excluded.method, note = excluded.note, approved_by = excluded.approved_by",
                (activity_id, d, to_db_text(p, 2), method, clean(note) or "", clean(approved_by) or ""))
            last = self.conn.execute("SELECT as_of, physical_pct FROM activity_progress_snapshots WHERE activity_id = ?"
                                     " ORDER BY as_of DESC LIMIT 1", (activity_id,)).fetchone()
            self.conn.execute("UPDATE ws_activities SET progress = ?, progress_date = ? WHERE id = ?",
                              (last["physical_pct"], last["as_of"], activity_id))
            audit.log(self.conn, "ws_activity", activity_id, "GUNCELLE", new={"progress": str(p), "as_of": d, "method": method},
                      note="İlerleme snapshot")

    def progress_at(self, activity_id: int, as_of: str) -> Decimal | None:
        r = self.conn.execute("SELECT physical_pct FROM activity_progress_snapshots WHERE activity_id = ? AND as_of <= ?"
                              " ORDER BY as_of DESC LIMIT 1", (activity_id, as_of)).fetchone()
        return from_db_text(r["physical_pct"]) if r else None

    def progress_history(self, activity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM activity_progress_snapshots WHERE activity_id = ? ORDER BY as_of",
                                 (activity_id,)).fetchall()

    # --- kapsam: faaliyet ↔ keşif kalemi ---------------------------------------
    def set_scope(self, activity_id: int, estimate_item_id: int, ratio, note: str = "") -> None:
        a = self._activity(activity_id)
        item = self.conn.execute("SELECT i.id, e.project_id FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id"
                                 " WHERE i.id = ?", (estimate_item_id,)).fetchone()
        if item is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        if item["project_id"] != a["project_id"]:
            raise ValidationError("Faaliyet ve keşif kalemi farklı projelere ait; eşleştirilemez.")
        r = _ratio(ratio, "Kapsam oranı")
        others = sum((from_db_text(x["ratio"]) for x in self.conn.execute(
            "SELECT ratio FROM activity_scope_allocations WHERE estimate_item_id = ? AND activity_id <> ?",
            (estimate_item_id, activity_id))), ZERO)
        if others + r > HUNDRED:
            raise ValidationError(f"Kalemin toplam tahsisi %{others + r} olur; en çok %100 olabilir (diğer faaliyetlere %{others} ayrılmış).")
        with transaction(self.conn):
            self.conn.execute("INSERT INTO activity_scope_allocations(activity_id, estimate_item_id, ratio, note) VALUES (?, ?, ?, ?)"
                              " ON CONFLICT(activity_id, estimate_item_id) DO UPDATE SET ratio = excluded.ratio, note = excluded.note",
                              (activity_id, estimate_item_id, to_db_text(r, 2), clean(note) or ""))
            audit.log(self.conn, "ws_activity", activity_id, "GUNCELLE", new={"scope_item": estimate_item_id, "ratio": str(r)},
                      note="Kapsam tahsisi")

    def remove_scope(self, activity_id: int, estimate_item_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM activity_scope_allocations WHERE activity_id = ? AND estimate_item_id = ?",
                              (activity_id, estimate_item_id))

    def scope_report(self, project_id: int) -> list[dict]:
        names = {a.id: a.name for a in self.plan.activities(project_id)}
        rows = []
        for it in self.conn.execute(
                "SELECT i.* FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id WHERE e.project_id = ?"
                " ORDER BY e.id, i.line_no", (project_id,)):
            allocs = self.conn.execute("SELECT * FROM activity_scope_allocations WHERE estimate_item_id = ? ORDER BY id",
                                       (it["id"],)).fetchall()
            total = sum((from_db_text(a["ratio"]) for a in allocs), ZERO)
            qty = from_db_text(it["quantity"])
            rows.append({"item_id": it["id"], "line_no": it["line_no"], "code": it["raw_code"] or "",
                         "description": it["raw_description"] or "", "unit": it["raw_unit"] or "", "quantity": qty,
                         "allocated_pct": total, "unallocated_pct": HUNDRED - total,
                         "allocated_qty": quantize(qty * total / HUNDRED, 3), "warning": "Eşleşmemiş" if total == 0 else "",
                         "activities": [(names.get(a["activity_id"], "?"), from_db_text(a["ratio"])) for a in allocs]})
        return rows

    def activity_scope(self, activity_id: int) -> list[dict]:
        return [{"item_id": r["estimate_item_id"], "ratio": from_db_text(r["ratio"]), "description": r["raw_description"],
                 "unit": r["raw_unit"], "quantity": from_db_text(r["quantity"]),
                 "qty": quantize(from_db_text(r["quantity"]) * from_db_text(r["ratio"]) / HUNDRED, 3)}
                for r in self.conn.execute(
                    "SELECT s.estimate_item_id, s.ratio, i.raw_description, i.raw_unit, i.quantity FROM activity_scope_allocations s"
                    " JOIN estimate_items i ON i.id = s.estimate_item_id WHERE s.activity_id = ? ORDER BY i.line_no", (activity_id,))]

    # --- maliyet tahsisi --------------------------------------------------------
    def _lines(self, project_id: int, as_of: str | None = None) -> list[ActualLine]:
        return [ln for ln in self.costs.actual_lines(project_id) if ln.source_id is not None and (not as_of or ln.date <= as_of)]

    def set_cost_allocation(self, project_id: int, source_type: str, source_id: int, activity_id: int, ratio, note: str = "") -> None:
        if source_type not in SOURCE_TYPES:
            raise ValidationError("Geçersiz maliyet kaynağı türü.")
        a = self._activity(activity_id)
        if a["project_id"] != project_id:
            raise ValidationError("Faaliyet bu projeye ait değil.")
        if not any(ln.source_type == source_type and ln.source_id == source_id for ln in self._lines(project_id)):
            raise ValidationError("Maliyet kaynağı bu projede bulunamadı (başka projeye ait ya da geçerli değil).")
        r = _ratio(ratio, "Tahsis oranı")
        others = sum((from_db_text(x["ratio"]) for x in self.conn.execute(
            "SELECT ratio FROM cost_activity_allocations WHERE source_type = ? AND source_id = ? AND activity_id <> ?",
            (source_type, source_id, activity_id))), ZERO)
        if others + r > HUNDRED:
            raise ValidationError(f"Kaynağın toplam tahsisi %{others + r} olur; en çok %100 olabilir.")
        with transaction(self.conn):
            self.conn.execute("INSERT INTO cost_activity_allocations(source_type, source_id, activity_id, ratio, note)"
                              " VALUES (?, ?, ?, ?, ?) ON CONFLICT(source_type, source_id, activity_id) DO UPDATE SET"
                              " ratio = excluded.ratio, note = excluded.note",
                              (source_type, source_id, activity_id, to_db_text(r, 2), clean(note) or ""))
            self._sync_entry_link(source_type, source_id)
            audit.log(self.conn, "ws_activity", activity_id, "GUNCELLE",
                      new={"cost_source": f"{source_type}:{source_id}", "ratio": str(r)}, note="Maliyet tahsisi")

    def remove_cost_allocation(self, source_type: str, source_id: int, activity_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM cost_activity_allocations WHERE source_type = ? AND source_id = ? AND activity_id = ?",
                              (source_type, source_id, activity_id))
            self._sync_entry_link(source_type, source_id)

    def _sync_entry_link(self, source_type: str, source_id: int) -> None:
        """Eski `ac_cost_entries.activity_id` sütununu tahsisle uyumlu tutar (tek %100 tahsis → o faaliyet, aksi hâlde boş)."""
        if source_type != "GIDER":
            return
        rows = self.conn.execute("SELECT activity_id, ratio FROM cost_activity_allocations WHERE source_type = 'GIDER' AND"
                                 " source_id = ?", (source_id,)).fetchall()
        one = rows[0]["activity_id"] if len(rows) == 1 and from_db_text(rows[0]["ratio"]) == HUNDRED else None
        self.conn.execute("UPDATE ac_cost_entries SET activity_id = ? WHERE id = ?", (one, source_id))

    def _implicit_package_map(self, project_id: int) -> dict[int, int]:
        """Taşeron sözleşme kimliği → tek bağlı faaliyet (paket bağlantısı); birden çok faaliyet aynı paketteyse örtük tahsis yok."""
        out: dict[int, list[int]] = {}
        for a in self.plan.activities(project_id):
            if a.package_id is None:
                continue
            row = self.conn.execute("SELECT c.id FROM pp_contracts c JOIN sub_packages k ON k.selected_bid_id = c.sub_bid_id"
                                    " WHERE k.id = ?", (a.package_id,)).fetchone()
            if row:
                out.setdefault(row["id"], []).append(a.id)
        return {cid: ids[0] for cid, ids in out.items() if len(ids) == 1}

    def cost_sources(self, project_id: int, as_of: str | None = None) -> list[CostSource]:
        explicit: dict[tuple[str, int], list[sqlite3.Row]] = {}
        for r in self.conn.execute("SELECT c.* FROM cost_activity_allocations c JOIN ws_activities a ON a.id = c.activity_id"
                                   " WHERE a.project_id = ? ORDER BY c.id", (project_id,)):
            explicit.setdefault((r["source_type"], r["source_id"]), []).append(r)
        payment_contract = {r["id"]: r["contract_id"] for r in self.conn.execute(
            "SELECT p.id, p.contract_id FROM pp_payments p JOIN pp_contracts c ON c.id = p.contract_id WHERE c.project_id = ?",
            (project_id,))}
        implicit = self._implicit_package_map(project_id)
        out = []
        for ln in self._lines(project_id, as_of):
            src = CostSource(ln.source_type, ln.source_id, ln.date, ln.description, ln.amount)
            rows = explicit.get((ln.source_type, ln.source_id), [])
            if rows:
                ratios = [from_db_text(r["ratio"]) for r in rows]
                src.allocations = [AllocRow(r["activity_id"], rt, amt) for r, rt, amt in zip(rows, ratios, split_amount(ln.amount, ratios))]
            elif ln.source_type == "TASERON_HAKEDIS" and payment_contract.get(ln.source_id) in implicit:
                src.allocations = [AllocRow(implicit[payment_contract[ln.source_id]], HUNDRED, ln.amount, implicit=True)]
            out.append(src)
        return out

    def activity_allocated(self, project_id: int, as_of: str | None = None) -> dict[int, dict[str, Decimal]]:
        """Faaliyet → {kaynak türü: tahsisli tutar}."""
        out: dict[int, dict[str, Decimal]] = {}
        for s in self.cost_sources(project_id, as_of):
            for a in s.allocations:
                d = out.setdefault(a.activity_id, {})
                d[s.source_type] = d.get(s.source_type, ZERO) + a.amount
        return out

    def reconcile(self, project_id: int, as_of: str | None = None) -> dict:
        """Mutabakat: kaynak toplamı = tahsisli + tahsis edilmemiş (kuruş); kaynak toplamı tahsisten bağımsızdır."""
        srcs = self.cost_sources(project_id, as_of)
        total = sum((s.amount for s in srcs), ZERO)
        allocated = sum((s.allocated for s in srcs), ZERO)
        over = [s for s in srcs if s.allocated > s.amount]
        return {"source_total": total, "allocated": allocated, "unallocated": total - allocated,
                "unallocated_sources": [s for s in srcs if s.unallocated != 0], "over_allocated": over,
                "ok": not over and total == allocated + (total - allocated)}
