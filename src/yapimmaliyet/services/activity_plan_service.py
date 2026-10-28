"""Faaliyet bazlı iş programı ve maliyet analizi: planlanan / gerçekleşen maliyet, dönemsel bütçe, sapma, ilerleme.

Kurallar (belirli bir dosya biçimine bağlı değildir; gerçek iş programı örnekleri gelince içe aktarma eklenebilir):
- Planlanan maliyet faaliyetin başlangıç–bitiş tarihleri arasına gün oranıyla aylara dağıtılır (kayan nokta yok; ay payları
  kuruşa yuvarlanır, son aya kalan eklenir, toplam planlanan maliyete eşittir).
- Gerçekleşen maliyet `CostTrackingService.actual_lines` kaynaklarından (KDV hariç) tarihe göre aylanır; kopyalanmaz.
  Maliyet ≠ hakediş ≠ nakit çıkışı: bu rapor yalnız maliyet tarafıdır.
- Kazanılmış değer = Σ planlanan maliyet × ilerleme. Faaliyet bazında gerçekleşen maliyet izlenmediğinden maliyet
  performansı (CPI) proje düzeyinde hesaplanır: kazanılmış değer ÷ gerçekleşen maliyet; ilerleme girilmemişse boştur.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.cost_tracking_service import CostTrackingService
from yapimmaliyet.services.validators import clean, require, validate_iso_date

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Activity:
    id: int
    project_id: int
    name: str
    discipline: str | None
    package_id: int | None
    estimate_item_id: int | None
    contract_id: int | None
    planned_start: str
    planned_end: str
    planned_qty: Decimal | None
    unit: str | None
    planned_cost: Decimal
    progress: Decimal
    progress_date: str | None
    notes: str | None
    external_code: str | None = None
    wbs_code: str | None = None

    @property
    def earned(self) -> Decimal:
        return quantize(self.planned_cost * self.progress / HUNDRED, 2)


@dataclass(frozen=True)
class PeriodRow:
    period: str
    planned: Decimal
    actual: Decimal
    cum_planned: Decimal
    cum_actual: Decimal

    @property
    def variance(self) -> Decimal:
        """Dönemsel sapma: gerçekleşen − planlanan (artı: planın üzerinde harcama)."""
        return self.actual - self.planned


@dataclass
class PlanReport:
    rows: list[PeriodRow]
    planned_total: Decimal
    actual_total: Decimal
    earned_value: Decimal
    progress_weighted: Decimal | None     # % (planlanan maliyet ağırlıklı)

    @property
    def variance(self) -> Decimal:
        return self.actual_total - self.planned_total

    @property
    def cpi(self) -> Decimal | None:
        if self.actual_total <= 0 or self.earned_value <= 0:
            return None
        return quantize(self.earned_value / self.actual_total, 2)


def month_shares(start: str, end: str, total: Decimal) -> dict[str, Decimal]:
    """Tutarı gün oranıyla aylara böler; kuruşa yuvarlar, farkı son aya ekler."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    days = (d1 - d0).days + 1
    per: dict[str, int] = {}
    d = d0
    while d <= d1:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        stop = min(last, d1)
        per[d.strftime("%Y-%m")] = (stop - d).days + 1
        d = stop + timedelta(days=1)
    out, acc = {}, ZERO
    keys = list(per)
    for k in keys[:-1]:
        out[k] = quantize(total * per[k] / days, 2)
        acc += out[k]
    out[keys[-1]] = total - acc
    return out


def _wbs(value) -> str | None:
    """WBS kodu: noktalı sayısal/alfasayısal düzey ('1', '1.2', '1.2.3'); boşsa None."""
    v = clean(value)
    if v is None:
        return None
    parts = v.split(".")
    if any(not p or not p.replace("-", "").replace("_", "").isalnum() for p in parts):
        raise ValidationError(f"WBS kodu geçersiz: '{v}' (örnek: 1.2.3).")
    return v


def _act(r: sqlite3.Row) -> Activity:
    return Activity(r["id"], r["project_id"], r["name"], r["discipline"], r["package_id"], r["estimate_item_id"],
                    r["contract_id"], r["planned_start"], r["planned_end"],
                    from_db_text(r["planned_qty"]) if r["planned_qty"] else None, r["unit"],
                    from_db_text(r["planned_cost"]), from_db_text(r["progress"]), r["progress_date"], r["notes"],
                    r["external_code"], r["wbs_code"])


@dataclass(frozen=True)
class ActivityCost:
    activity: Activity
    manual: Decimal            # faaliyete atanmış gider kayıtları (KDV hariç)
    subcontract: Decimal       # bağlı iş paketinin taşeron sözleşmesindeki geçerli hakedişler (KDV hariç)

    @property
    def actual(self) -> Decimal:
        return self.manual + self.subcontract

    @property
    def variance(self) -> Decimal:
        return self.actual - self.activity.planned_cost

    @property
    def cpi(self) -> Decimal | None:
        """Faaliyet düzeyinde maliyet performansı; gerçekleşen ya da ilerleme yoksa boş."""
        if self.actual <= 0 or self.activity.earned <= 0:
            return None
        return quantize(self.activity.earned / self.actual, 2)


class ActivityPlanService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.costs = CostTrackingService(conn)

    def activities(self, project_id: int) -> list[Activity]:
        return [_act(r) for r in self.conn.execute(
            "SELECT * FROM ws_activities WHERE project_id = ? ORDER BY planned_start, id", (project_id,))]

    def _values(self, d: dict) -> dict:
        start = validate_iso_date(d.get("planned_start"), "Planlanan başlangıç", required=True)
        end = validate_iso_date(d.get("planned_end"), "Planlanan bitiş", required=True)
        if end < start:
            raise ValidationError("Planlanan bitiş, başlangıçtan önce olamaz.")
        try:
            cost = to_decimal(d.get("planned_cost"))
            prog = to_decimal(d.get("progress") or 0)
            qty = to_decimal(d["planned_qty"]) if d.get("planned_qty") not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise ValidationError("Tutar, miktar ve ilerleme sayı olmalıdır.") from exc
        if cost < 0 or (qty is not None and qty < 0):
            raise ValidationError("Planlanan maliyet ve miktar negatif olamaz.")
        if not ZERO <= prog <= HUNDRED:
            raise ValidationError("İlerleme yüzdesi 0 ile 100 arasında olmalıdır.")
        disc = d.get("discipline") or None
        if disc and disc not in DISCIPLINES:
            raise ValidationError("Geçersiz disiplin.")
        progress_date = validate_iso_date(d.get("progress_date"), "İlerleme tarihi")
        if prog > 0 and progress_date is None:
            progress_date = date.today().isoformat()      # tarihsiz ilerleme EVM'de "girilmemiş" sayılırdı; giriş günü ölçüm tarihidir
        return dict(name=require(d.get("name"), "Faaliyet adı"), discipline=disc, package_id=d.get("package_id"),
                    estimate_item_id=d.get("estimate_item_id"), contract_id=d.get("contract_id"), planned_start=start,
                    planned_end=end, planned_qty=to_db_text(qty, 3) if qty is not None else None,
                    unit=clean(d.get("unit")), planned_cost=to_db_text(cost, 2), progress=to_db_text(prog, 2),
                    progress_date=progress_date,
                    notes=clean(d.get("notes")), external_code=clean(d.get("external_code")),
                    wbs_code=_wbs(d.get("wbs_code")))

    def add(self, project_id: int, data: dict) -> int:
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise ValidationError("Proje bulunamadı.")
        v = self._values(data)
        cols = ["project_id"] + list(v)
        self._check_code(project_id, v.get("external_code"))
        with transaction(self.conn):
            aid = self.conn.execute(f"INSERT INTO ws_activities({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    [project_id] + list(v.values())).lastrowid
            self._log_progress(aid, v)
            audit.log(self.conn, "ws_activity", aid, "EKLE", new={"name": v["name"], "cost": v["planned_cost"]})
        return aid

    def _log_progress(self, activity_id: int, v: dict) -> None:
        """Faaliyet formundaki ilerleme, tarihli ölçüm (snapshot) olarak da kaydedilir: PV/EV/CPI/SPI ile faaliyet ekranı aynı
        veriyi okur. Alan, en son ölçümle eşitlenir (eski tarihli giriş son ölçümü bozmaz)."""
        if v.get("progress_date") is None:
            return
        self.conn.execute(
            "INSERT INTO activity_progress_snapshots(activity_id, as_of, physical_pct, method, note) VALUES (?, ?, ?, 'MANUEL_YUZDE',"
            " 'Faaliyet kaydından') ON CONFLICT(activity_id, as_of) DO UPDATE SET physical_pct = excluded.physical_pct",
            (activity_id, v["progress_date"], v["progress"]))
        last = self.conn.execute("SELECT as_of, physical_pct FROM activity_progress_snapshots WHERE activity_id = ?"
                                 " ORDER BY as_of DESC LIMIT 1", (activity_id,)).fetchone()
        self.conn.execute("UPDATE ws_activities SET progress = ?, progress_date = ? WHERE id = ?",
                          (last["physical_pct"], last["as_of"], activity_id))

    def _check_code(self, project_id: int, code: str | None, exclude: int | None = None) -> None:
        if code and self.conn.execute("SELECT 1 FROM ws_activities WHERE project_id = ? AND external_code = ? AND id IS NOT ?",
                                      (project_id, code, exclude)).fetchone():
            raise ValidationError(f"'{code}' faaliyet kodu bu projede zaten var.")

    def add_many(self, project_id: int, rows: list[dict]) -> list[int]:
        """Birden çok faaliyeti tek işlemde ekler; biri geçersizse hiçbiri yazılmaz."""
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise ValidationError("Proje bulunamadı.")
        values = [self._values(r) for r in rows]
        ids = []
        with transaction(self.conn):
            for v in values:
                cols = ["project_id"] + list(v)
                ids.append(self.conn.execute(
                    f"INSERT INTO ws_activities({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [project_id] + list(v.values())).lastrowid)
                self._log_progress(ids[-1], v)
            audit.log(self.conn, "ws_activity", None, "TOPLU", new={"project": project_id, "count": len(ids)},
                      note="İş programı dosyasından aktarım")
        return ids

    def preview_import(self, project_id: int, path, mapping: dict[str, int] | None = None, mode: str = "ATLA"):
        """İş programı dosyasını okur ve projeyle karşılaştırır; hiçbir şey kaydetmez.

        Mevcut kayıtlar (önce faaliyet kodu, kod yoksa ad + tarihler eşleşmesi): mode 'ATLA' → atlanır (tekrar import
        çoğaltmaz); 'GUNCELLE' → kod eşleşenler güncellenir (kodsuz eşleşmeler yine atlanır).
        """
        from yapimmaliyet.domain.text_tr import normalize_search
        from yapimmaliyet.services.schedule_import import read_schedule_file

        if mode not in ("ATLA", "GUNCELLE"):
            raise ValidationError("Geçersiz tekrar-import modu.")
        res = read_schedule_file(path, mapping)
        existing = self.activities(project_id)
        by_code = {a.external_code: a for a in existing if a.external_code}
        by_key = {(normalize_search(a.name).strip(), a.planned_start, a.planned_end): a for a in existing}
        keep = []
        for r in res.rows:
            d = r.data
            hit = by_code.get(d["external_code"]) if d.get("external_code") else None
            if hit is None and not d.get("external_code"):
                hit = by_key.get((normalize_search(d["name"]).strip(), d["planned_start"], d["planned_end"]))
            if hit is None:
                keep.append(r)
            elif mode == "GUNCELLE" and d.get("external_code"):
                res.updates.append((hit.id, r))
            else:
                res.skipped.append(r)
        res.rows = keep
        return res

    def import_file(self, project_id: int, path, accept_rejects: bool = False, mapping: dict[str, int] | None = None,
                    mode: str = "ATLA") -> list[int]:
        """Dosyadan faaliyet aktarır (tek işlem: biri geçersizse hiçbiri yazılmaz). Reddedilen satır varsa `accept_rejects`
        açıkça verilmedikçe aktarılmaz. Her başarılı aktarım batch kaydı (sağlama toplamı, eşleme, sayılar) ve faaliyet
        başına kaynak satır izi bırakır."""
        import hashlib
        import json
        from pathlib import Path as _P

        res = self.preview_import(project_id, path, mapping, mode)
        if not res.rows and not res.updates:
            raise ValidationError("Dosyadan aktarılacak yeni faaliyet yok. " + res.summary())
        if res.rejected and not accept_rejects:
            raise ValidationError("Dosyada reddedilen satırlar var; onaylamadan aktarılamaz. " + res.summary())
        checksum = hashlib.sha256(_P(path).read_bytes()).hexdigest()
        new_values = [self._values(r.data) for r in res.rows]
        upd_values = [(aid, self._values(r.data), r) for aid, r in res.updates]
        ids: list[int] = []
        with transaction(self.conn):
            batch = self.conn.execute(
                "INSERT INTO schedule_import_batches(project_id, source_name, checksum, mapping_json, mode, status, row_count,"
                " added_count, updated_count, skipped_count) VALUES (?, ?, ?, ?, ?, 'TAMAM', ?, ?, ?, ?)",
                (project_id, _P(path).name, checksum, json.dumps(mapping or {}), mode, res.source_rows, len(new_values),
                 len(upd_values), len(res.skipped))).lastrowid
            for v, r in zip(new_values, res.rows):
                cols = ["project_id", "import_batch_id", "source_ref"] + list(v)
                ids.append(self.conn.execute(
                    f"INSERT INTO ws_activities({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [project_id, batch, r.ref] + list(v.values())).lastrowid)
                self._log_progress(ids[-1], v)
            for aid, v, r in upd_values:
                self.conn.execute(f"UPDATE ws_activities SET {', '.join(k + ' = ?' for k in v)}, import_batch_id = ?, source_ref = ?"
                                  " WHERE id = ?", [*v.values(), batch, r.ref, aid])
                self._log_progress(aid, v)
            audit.log(self.conn, "ws_activity", None, "TOPLU",
                      new={"project": project_id, "batch": batch, "added": len(new_values), "updated": len(upd_values),
                           "skipped": len(res.skipped)}, note="İş programı dosyasından aktarım")
        return ids + [aid for aid, _v, _r in upd_values]

    def import_batches(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM schedule_import_batches WHERE project_id = ? ORDER BY id DESC",
                                 (project_id,)).fetchall()

    def update(self, activity_id: int, data: dict) -> None:
        old = self.conn.execute("SELECT * FROM ws_activities WHERE id = ?", (activity_id,)).fetchone()
        if old is None:
            raise NotFoundError("Faaliyet bulunamadı.")
        v = self._values(data)
        self._check_code(old["project_id"], v.get("external_code"), activity_id)
        with transaction(self.conn):
            self.conn.execute(f"UPDATE ws_activities SET {', '.join(k + ' = ?' for k in v)} WHERE id = ?",
                              list(v.values()) + [activity_id])
            if (v["progress"], v["progress_date"]) != (old["progress"], old["progress_date"]):
                self._log_progress(activity_id, v)
            audit.log(self.conn, "ws_activity", activity_id, "GUNCELLE",
                      old={"cost": old["planned_cost"], "progress": old["progress"]},
                      new={"cost": v["planned_cost"], "progress": v["progress"]})

    def delete(self, activity_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM ws_activities WHERE id = ?", (activity_id,))
            audit.log(self.conn, "ws_activity", activity_id, "SIL")

    def activity_costs(self, project_id: int) -> tuple[list[ActivityCost], Decimal]:
        """Faaliyet bazlı gerçekleşen maliyet ve tahsis edilmemiş kalan.

        Tahsisler `ScheduleControlService` üzerindendir (oranlı; kaynak tutarı kopyalanmaz): gider/teslim → `manual`,
        taşeron hakedişi (açık tahsis ya da tek bağlı iş paketi) → `subcontract`. Toplam, proje gerçekleşenine eşittir:
        Σ faaliyet + tahsis edilmemiş = `actual_lines` toplamı.
        """
        from yapimmaliyet.services.schedule_control_service import ScheduleControlService

        ctl = ScheduleControlService(self.conn)
        alloc = ctl.activity_allocated(project_id)
        out = []
        for a in self.activities(project_id):
            d = alloc.get(a.id, {})
            out.append(ActivityCost(a, d.get("GIDER", ZERO) + d.get("TESLIM", ZERO), d.get("TASERON_HAKEDIS", ZERO)))
        total = sum((ln.amount for ln in self.costs.actual_lines(project_id)), ZERO)
        return out, total - sum((c.actual for c in out), ZERO)

    def effective_months(self, a: Activity) -> dict[str, Decimal]:
        """Faaliyetin aylık planı: manuel aylık dağılım varsa o, yoksa gün oranlı dağılım."""
        manual = {r["period"]: from_db_text(r["amount"]) for r in self.conn.execute(
            "SELECT period, amount FROM activity_monthly_plan WHERE activity_id = ? ORDER BY period", (a.id,))}
        return manual or month_shares(a.planned_start, a.planned_end, a.planned_cost)

    def set_monthly_plan(self, activity_id: int, months: dict[str, object]) -> None:
        """Manuel aylık dağılım: dönemler faaliyet aralığında, toplam planlanan maliyete TAM eşit olmalıdır."""
        a = next((x for x in self._all_activities() if x.id == activity_id), None)
        if a is None:
            raise NotFoundError("Faaliyet bulunamadı.")
        first, last = a.planned_start[:7], a.planned_end[:7]
        parsed: dict[str, Decimal] = {}
        for k, v in months.items():
            if not (len(k) == 7 and k[4] == "-" and first <= k <= last):
                raise ValidationError(f"Dönem {k} faaliyet aralığının ({first} – {last}) dışında.")
            try:
                amt = to_decimal(v)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{k} tutarı geçerli bir sayı değil.") from exc
            if amt < 0:
                raise ValidationError("Aylık tutar negatif olamaz.")
            parsed[k] = quantize(amt, 2)
        if sum(parsed.values(), ZERO) != a.planned_cost:
            raise ValidationError(f"Aylık dağılım toplamı ({sum(parsed.values(), ZERO)}) planlanan maliyete ({a.planned_cost}) eşit olmalı.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM activity_monthly_plan WHERE activity_id = ?", (activity_id,))
            for k, amt in parsed.items():
                self.conn.execute("INSERT INTO activity_monthly_plan(activity_id, period, amount) VALUES (?, ?, ?)",
                                  (activity_id, k, to_db_text(amt, 2)))
            audit.log(self.conn, "ws_activity", activity_id, "GUNCELLE", new={"monthly": {k: str(v) for k, v in parsed.items()}},
                      note="Manuel aylık plan")

    def clear_monthly_plan(self, activity_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM activity_monthly_plan WHERE activity_id = ?", (activity_id,))

    def _all_activities(self) -> list[Activity]:
        return [_act(r) for r in self.conn.execute("SELECT * FROM ws_activities")]

    def monthly_plan(self, project_id: int, discipline: str | None = None) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for a in self.activities(project_id):
            if discipline and a.discipline != discipline:
                continue
            for m, v in self.effective_months(a).items():
                out[m] = out.get(m, ZERO) + v
        return out

    def report(self, project_id: int, discipline: str | None = None) -> PlanReport:
        plan = self.monthly_plan(project_id, discipline)
        actual: dict[str, Decimal] = {}
        if not discipline:   # gerçekleşen maliyet disiplin bazında izlenmediği için süzgeçte boş bırakılır
            for ln in self.costs.actual_lines(project_id):
                actual[ln.date[:7]] = actual.get(ln.date[:7], ZERO) + ln.amount
        months = sorted(set(plan) | set(actual))
        rows, cp, ca = [], ZERO, ZERO
        for m in months:
            cp += plan.get(m, ZERO)
            ca += actual.get(m, ZERO)
            rows.append(PeriodRow(m, plan.get(m, ZERO), actual.get(m, ZERO), cp, ca))
        acts = [a for a in self.activities(project_id) if not discipline or a.discipline == discipline]
        planned_total = sum((a.planned_cost for a in acts), ZERO)
        earned = sum((a.earned for a in acts), ZERO)
        weighted = quantize(earned * HUNDRED / planned_total, 2) if planned_total > 0 else None
        return PlanReport(rows, planned_total, ca, earned, weighted)
