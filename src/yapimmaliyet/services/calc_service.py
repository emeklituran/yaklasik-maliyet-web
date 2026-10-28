"""Hesap profilleri, taslak hesap ve kesinleştirme (değişmez kayıt).

- Taslak hesap her çağrıda güncel verilerle yeniden yapılır; veritabanına yazılmaz.
- Kesinleştirme; bütün kalemler onaylı ve birim farkları kabul edilmiş olduğunda yapılır.
  Sonuç (satırlar, parametreler, profil, toplamlar) JSON olarak donmuş biçimde saklanır;
  sonradan kütüphane ya da keşif değişse de bu kayıt değişmez.
- Profiller sürümlüdür; içe aktarılan profil yeni sürüm olarak eklenir, eskiler silinmez.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.migrator import current_version
from yapimmaliyet.db.repositories import audit, calculations, estimate_items, projects
from yapimmaliyet.db.repositories.calculations import Profile, SnapshotInfo
from yapimmaliyet.domain.calc import CalcInput, CalcKind, CalcResult, PriceOrigin, ProfileParams, calculate
from yapimmaliyet.domain.indexing import GENERAL_SERIES, ym_ratio
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.tender import TenderLine, effective_method, summarize
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.fx_service import FxService
from yapimmaliyet.services.index_service import IndexService
from yapimmaliyet.services.validators import require, validate_iso_date

PROFILE_FILE_FORMAT = "yapimmaliyet-hesap-profili"
PROFILE_FILE_VERSION = 1
SNAPSHOT_SCHEMA = 1


def _s(value) -> str | None:
    return None if value is None else str(value)


def _index_payload(ln) -> dict | None:
    r = ln.input.index_ratio
    if not ln.index_updated or r is None:
        return None
    return {"series": r.series, "current_period": r.numerator_period, "current_value": _s(r.numerator),
            "base_period": r.denominator_period, "base_value": _s(r.denominator), "factor": _s(r.factor),
            "base_price": _s(ln.base_price)}


def _tender_payload(r: CalcResult, model: str) -> dict:
    """İhale modeli ve disiplin / teklif yöntemi dökümü (hesap tutarlarını değiştirmez)."""
    lines = [TenderLine(ln.input.item_id, ln.input.line_no, ln.input.code, ln.input.description, ln.input.unit,
                        ln.input.quantity, ln.input.discipline, effective_method(model, ln.input.pricing_method),
                        ln.unit_price, ln.amount) for ln in r.lines]
    s = summarize(model, lines)

    def parts(ps):
        return [{"key": x.key, "label": x.label, "count": x.count, "amount": _s(x.amount), "unpriced": x.unpriced}
                for x in ps]
    return {"model": model, "model_label": s.model_label, "by_discipline": parts(s.by_discipline),
            "by_method": parts(s.by_method), "notes": list(s.notes)}


def _currency_payload(r: CalcResult, rate) -> dict | None:
    """Keşfe sabitlenen kurla KDV hariç toplamın döviz karşılığı; kur kaydı aynen saklanır (sonradan değişmez)."""
    if rate is None:
        return None
    return {"currency": rate.currency, "rate": _s(rate.rate), "rate_type": rate.rate_type, "rate_date": rate.rate_date,
            "source": rate.source, "source_url": rate.source_url, "bulletin_no": rate.bulletin_no,
            "fetched_at": rate.fetched_at, "rate_id": rate.id,
            "total_excl_vat": _s(quantize(r.total_excl_vat / rate.rate, 2))}


class CalcService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- profiller ---------------------------------------------------------
    def profiles(self, kind: CalcKind | None = None) -> list[Profile]:
        return calculations.list_profiles(self.conn, kind.value if kind else None)

    def get_profile(self, profile_id: int) -> Profile:
        p = calculations.get_profile(self.conn, profile_id)
        if p is None:
            raise NotFoundError("Hesap profili bulunamadı.")
        return p

    def export_profile(self, profile_id: int, path: Path) -> None:
        p = self.get_profile(profile_id)
        data = {"format": PROFILE_FILE_FORMAT, "format_version": PROFILE_FILE_VERSION, "kind": p.kind,
                "name": p.name, "version": p.version, "valid_from": p.valid_from,
                "legal_basis_text": p.legal_basis_text, "verified": p.verified, "params": p.params}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_profile(self, path: Path) -> int:
        """Dışarıdan temin edilen profili yeni sürüm olarak ekler. Doğrulama bayrağı dosyadan alınmaz."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValidationError("Profil dosyası okunamadı ya da geçerli JSON değil.", detail=str(exc)) from exc
        if not isinstance(data, dict) or data.get("format") != PROFILE_FILE_FORMAT:
            raise ValidationError("Bu dosya bir YapımMaliyet hesap profili değil.")
        if data.get("format_version") != PROFILE_FILE_VERSION:
            raise ValidationError("Profil dosyası bu programın desteklemediği bir sürümde.")
        try:
            kind = CalcKind(data.get("kind"))
        except ValueError as exc:
            raise ValidationError("Profil türü geçersiz (REFERANS ya da YAKLASIK olmalı).") from exc
        name = require(data.get("name"), "Profil adı")
        try:
            params = ProfileParams.from_json(data.get("params", {}))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        legal = require(data.get("legal_basis_text"), "Dayanak / açıklama metni")
        valid_from = validate_iso_date(data.get("valid_from"), "Yürürlük tarihi")
        with transaction(self.conn):
            version = calculations.next_profile_version(self.conn, name)
            pid = calculations.insert_profile(self.conn, kind.value, name, version, params.to_json(), legal, False,
                                              valid_from, Path(path).name)
            audit.log(self.conn, "calc_profile", pid, "EKLE", new={"name": name, "version": version},
                      note="Profil dosyadan içe aktarıldı; doğrulanmamış olarak işaretlendi.")
        return pid

    def mark_verified(self, profile_id: int, verified: bool) -> None:
        self.get_profile(profile_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE calc_profiles SET verified = ? WHERE id = ?", (int(verified), profile_id))
            audit.log(self.conn, "calc_profile", profile_id, "GUNCELLE", new={"verified": verified},
                      note="Kullanıcı doğrulama durumunu değiştirdi")

    # --- hesap -------------------------------------------------------------
    def inputs(self, estimate_id: int, index_update: bool = False) -> tuple[list[CalcInput], tuple[str, ...]]:
        """Hesap girdileri ve kalem dışı sorunlar. Endeks güncellemesi istenirse her fiyatlı kalem için
        temel/güncel dönem ve endeks değerleri bulunur; eksikse kalem sorunlu sayılır."""
        estimate = projects.get_estimate(self.conn, estimate_id)
        extra: list[str] = []
        codes = {s.id: s.code for s in IndexService(self.conn).list_series()}
        lookup = IndexService(self.conn).lookup()
        if index_update and not estimate.ym_date:
            extra.append("Endeks güncellemesi için keşifte yaklaşık maliyet hesap tarihi girilmemiş.")
        out = []
        for it in estimate_items.list_items(self.conn, estimate_id):
            ratio = problem = None
            if index_update and estimate.ym_date and it.matched_price is not None:
                series = codes.get(it.index_series_id or estimate.index_series_id or -1, GENERAL_SERIES)
                if not it.price_valid_from:
                    problem = ("Fiyat kitabının yürürlük tarihi girilmemiş (endeks temel dönemi bulunamıyor)"
                               if it.matched_origin == "KITAP" else "Fiyat tarihi yok")
                else:
                    try:
                        ratio = ym_ratio(lookup, series, it.price_valid_from, estimate.ym_date)
                    except ValueError as exc:   # eksik endeks değeri ya da geçersiz tarih
                        problem = str(exc)
            out.append(CalcInput(
                it.id, it.line_no, it.work_group, it.code or it.matched_code, it.description or it.matched_description,
                it.unit or it.matched_unit, it.quantity, it.matched_price, PriceOrigin(it.matched_origin),
                it.source_label, it.price_date, it.user_price_override, it.match_status, it.unit_mismatch,
                it.unit_mismatch_ack, ratio, problem, it.discipline, it.pricing_method, it.price_authority,
                it.price_period))
        return out, tuple(extra)

    def calculate(self, estimate_id: int, profile_id: int) -> CalcResult:
        if projects.get_estimate(self.conn, estimate_id) is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        profile = self.get_profile(profile_id)
        if profile.kind not in CalcKind.__members__:
            raise ValidationError("Bu profil yaklaşık maliyet/referans hesabı için kullanılamaz.")
        params = ProfileParams.from_json(profile.params)
        kind = CalcKind(profile.kind)
        inputs, extra = self.inputs(estimate_id, params.index_update and kind is CalcKind.YAKLASIK)
        return calculate(kind, inputs, params, extra)

    def finalize(self, estimate_id: int, profile_id: int, name: str | None = None) -> int:
        """Hesabı kesinleştirir; değişmez kaydın kimliğini döndürür."""
        estimate = projects.get_estimate(self.conn, estimate_id)
        if estimate is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        profile = self.get_profile(profile_id)
        result = self.calculate(estimate_id, profile_id)
        if not result.lines:
            raise ConflictError("Keşifte kalem yok.")
        if not result.finalizable:
            shown = "\n".join(result.problems[:8])
            more = f"\n… ve {len(result.problems) - 8} sorun daha" if len(result.problems) > 8 else ""
            raise ConflictError(f"Hesap kesinleştirilemez; önce şu sorunları giderin:\n{shown}{more}")
        project = projects.get_project(self.conn, estimate.project_id)
        payload = self.payload(result, profile, estimate, project, name, FxService(self.conn).estimate_rate(estimate_id))
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with transaction(self.conn):
            calc_id = calculations.insert_calculation(self.conn, estimate_id, profile_id, profile.kind, name,
                                                      estimate.source_priority)
            snap = calculations.insert_snapshot(self.conn, calc_id, __version__, current_version(self.conn), text, sha)
            audit.log(self.conn, "calculation", calc_id, "EKLE",
                      new={"snapshot": snap, "total": result.total_excl_vat}, note="Hesap kesinleştirildi")
        return snap

    @staticmethod
    def payload(r: CalcResult, profile: Profile, estimate, project, name: str | None, fx_rate=None) -> dict:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": __version__,
            "name": name,
            "kind": r.kind.value,
            "kind_label": r.kind.label,
            "project": {"id": project.id, "name": project.name, "ikn": project.ikn,
                        "authority": project.authority_name, "offer_type": project.offer_type,
                        "tender_model": project.tender_model, "disciplines": project.disciplines},
            "estimate": {"id": estimate.id, "name": estimate.name, "revision": estimate.revision,
                         "ym_date": estimate.ym_date},
            "profile": {"id": profile.id, "name": profile.name, "version": profile.version,
                        "params": profile.params, "legal_basis_text": profile.legal_basis_text,
                        "verified": profile.verified},
            "lines": [{
                "line_no": ln.input.line_no, "work_group": ln.input.work_group, "code": ln.input.code,
                "description": ln.input.description, "unit": ln.input.unit, "quantity": _s(ln.input.quantity),
                "unit_price": _s(ln.unit_price), "amount": _s(ln.amount), "origin": ln.input.origin.value,
                "price_source": ln.input.price_source, "price_date": ln.input.price_date,
                "price_authority": ln.input.price_authority, "price_period": ln.input.price_period,
                "overridden": ln.overridden, "matched_price": _s(ln.input.matched_price),
                "unit_mismatch_accepted": ln.input.unit_mismatch and ln.input.unit_mismatch_ack,
                "index": _index_payload(ln), "discipline": ln.input.discipline,
                "pricing_method": effective_method(project.tender_model, ln.input.pricing_method),
            } for ln in r.lines],
            "tender": _tender_payload(r, project.tender_model),
            "currency": _currency_payload(r, fx_rate),
            "groups": [{"name": g.name, "count": g.count, "amount": _s(g.amount)} for g in r.groups],
            "totals": {"subtotal": _s(r.subtotal), "overhead_profit": _s(r.overhead_profit),
                       "total_excl_vat": _s(r.total_excl_vat), "vat": _s(r.vat), "total_incl_vat": _s(r.total_incl_vat)},
            "notes": r.notes,
        }

    # --- kesinleşmiş kayıtlar ---------------------------------------------
    def snapshots(self, estimate_id: int) -> list[SnapshotInfo]:
        return calculations.list_snapshots(self.conn, estimate_id)

    def snapshot_payload(self, snapshot_id: int) -> dict:
        row = calculations.get_snapshot(self.conn, snapshot_id)
        if row is None:
            raise NotFoundError("Kesinleşmiş hesap kaydı bulunamadı.")
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["sha256"]:
            raise ConflictError("Kesinleşmiş hesap kaydının bütünlük özeti uyuşmuyor; kayıt bozulmuş olabilir.")
        return json.loads(row["payload_json"])
