"""Özel poz iş akışları (doğrudan fiyat).

Kurallar:
- Özel poz kodları "ÖZ-" önekiyle başlar; resmî pozlarla karışmaz.
- Fiyat, birim ya da tarif değişikliği her zaman yeni sürüm oluşturur; önceki sürümler kilitlenir.
  Böylece eski sürüme bağlı hesaplar değişmez.
- Fiyatın dayanağı (kaynak açıklaması) zorunludur.
- Analizle fiyat: girdilerden hesaplanan fiyat ayrı sürüm türü (ANALIZ) olarak saklanır;
  doğrudan girilen fiyatla karışmaz.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import analyses, audit
from yapimmaliyet.db.repositories import custom_positions as repo
from yapimmaliyet.domain import analysis, sub_analysis
from yapimmaliyet.domain.analysis import AnalysisSpec
from yapimmaliyet.domain.models import CustomPosition, CustomVersion, PriceMethod
from yapimmaliyet.domain.sub_analysis import PositionNode
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean as _clean
from yapimmaliyet.services.validators import require as _require
from yapimmaliyet.services.validators import validate_iso_date, validate_price

STATUSES = ("AKTIF", "PASIF")


def _price_date(value: str) -> str:
    return validate_iso_date(value, "Fiyat tarihi", required=True)


def _normalise_code(code: str) -> str:
    code = normalize_code(code)
    if not code.startswith(repo.CODE_PREFIX):
        code = repo.CODE_PREFIX + code
    if len(code) <= len(repo.CODE_PREFIX):
        raise ValidationError("Özel poz kodu geçersiz.")
    return code


class CustomPositionService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def list(self, text: str = "", include_passive: bool = False) -> list[CustomPosition]:
        return repo.list_positions(self.conn, text, include_passive)

    def get(self, cp_id: int) -> CustomPosition:
        cp = repo.get(self.conn, cp_id)
        if cp is None:
            raise NotFoundError("Özel poz bulunamadı.")
        return cp

    def versions(self, cp_id: int) -> list[CustomVersion]:
        return repo.versions(self.conn, cp_id)

    def suggest_code(self) -> str:
        return repo.next_code(self.conn)

    def create(self, name: str, unit: str, description: str, price: Decimal, price_date: str,
               basis_text: str, category: str | None = None, code: str | None = None) -> int:
        name = _require(name, "İmalat adı")
        unit = _require(unit, "Birim")
        description = _clean(description) or name
        price = validate_price(price)
        price_date = _price_date(price_date)
        basis_text = _require(basis_text, "Fiyat dayanağı / kaynak açıklaması")
        code = _normalise_code(code) if _clean(code) else repo.next_code(self.conn)
        if repo.code_exists(self.conn, code):
            raise ConflictError(f"'{code}' kodlu bir özel poz zaten var.")
        with transaction(self.conn):
            cp_id = repo.insert_position(self.conn, code, name, _clean(category), description)
            repo.insert_direct_version(self.conn, cp_id, 1, description, unit, price, price_date, basis_text)
            audit.log(self.conn, "custom_position", cp_id, "EKLE",
                      new={"code": code, "name": name, "unit": unit, "price": price, "price_date": price_date})
        return cp_id

    def new_version(self, cp_id: int, description: str, unit: str, price: Decimal, price_date: str,
                    basis_text: str) -> int:
        cp = self.get(cp_id)
        unit = _require(unit, "Birim")
        description = _require(description, "Tarif")
        price = validate_price(price)
        price_date = _price_date(price_date)
        basis_text = _require(basis_text, "Fiyat dayanağı / kaynak açıklaması")
        next_no = (cp.current.version_no if cp.current else 0) + 1
        with transaction(self.conn):
            repo.lock_versions(self.conn, cp_id)
            vid = repo.insert_direct_version(self.conn, cp_id, next_no, description, unit, price,
                                             price_date, basis_text)
            repo.refresh_search_text(self.conn, cp_id)
            old = cp.current
            audit.log(self.conn, "custom_position", cp_id, "GUNCELLE",
                      old=None if old is None else {"version": old.version_no, "unit": old.unit,
                                                    "price": old.effective_price},
                      new={"version": next_no, "unit": unit, "price": price}, note="Yeni fiyat sürümü")
        return vid

    # --- analizle fiyat ----------------------------------------------------
    # Alt analiz fiyat politikası: alt analiz girdisinin birim fiyatı, kayıt (ve hesap) anında alt pozun
    # GÜNCEL sürümünden çözülür ve o sürümün analizine yazılır. Kaydedilmiş sürümler kilitlidir; alt poz
    # sonradan yeni sürüm alırsa üst pozun eski sürümleri ve onlara bağlı hesap kayıtları (donmuş
    # snapshot'lar) değişmez; üst poz yeni sürüm kaydedilince güncel fiyat alınır.
    def _load_node(self, cp_id: int) -> PositionNode:
        cp = repo.get(self.conn, cp_id)
        if cp is None or cp.current is None:
            raise ValidationError("Alt analiz olarak seçilen özel poz bulunamadı.")
        cur = cp.current
        spec = analyses.get_spec(self.conn, cur.id) if cur.price_method == PriceMethod.ANALIZ else None
        return PositionNode(cp.code, cur.effective_price, spec)

    def resolve_spec(self, spec: AnalysisSpec, root_id: int | None, root_code: str) -> AnalysisSpec:
        """Alt analiz girdilerini güncel fiyatlarla doldurur; döngü/derinlik hatasında ValidationError."""
        try:
            return sub_analysis.resolve_spec(spec, self._load_node, ((root_id if root_id else -1, root_code),))
        except sub_analysis.SubAnalysisError as exc:
            raise ValidationError(str(exc)) from exc

    def resolve_price(self, cp_id: int) -> Decimal:
        """Özel pozun güncel çözümlenmiş birim fiyatı (alt analizler dahil)."""
        try:
            return sub_analysis.resolve_unit_price(cp_id, self._load_node)
        except sub_analysis.SubAnalysisError as exc:
            raise ValidationError(str(exc)) from exc

    def _analysis(self, spec: AnalysisSpec, root_id: int | None = None, root_code: str = ""):
        """Hesaplar; (sonuç, alt analiz fiyatları çözülmüş spec) döndürür. Yazmadan önce çağrılır."""
        if not (spec.rule_source_text or "").strip():
            raise ValidationError("Genel gider ve kâr oranlarının dayanağı (kaynak açıklaması) yazılmalıdır.")
        resolved = self.resolve_spec(spec, root_id, root_code)
        try:
            return analysis.compute(resolved), resolved
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def create_with_analysis(self, name: str, unit: str, description: str, price_date: str, basis_text: str,
                             spec: AnalysisSpec, category: str | None = None, code: str | None = None) -> int:
        name, unit = _require(name, "İmalat adı"), _require(unit, "Birim")
        description = _clean(description) or name
        price_date = _price_date(price_date)
        basis_text = _require(basis_text, "Fiyat dayanağı / kaynak açıklaması")
        code = _normalise_code(code) if _clean(code) else repo.next_code(self.conn)
        if repo.code_exists(self.conn, code):
            raise ConflictError(f"'{code}' kodlu bir özel poz zaten var.")
        result, spec = self._analysis(spec, None, code)
        with transaction(self.conn):
            cp_id = repo.insert_position(self.conn, code, name, _clean(category), description)
            vid = repo.insert_analysis_version(self.conn, cp_id, 1, description, unit, result.unit_price,
                                               price_date, basis_text)
            analyses.insert(self.conn, vid, spec)
            audit.log(self.conn, "custom_position", cp_id, "EKLE",
                      new={"code": code, "method": "ANALIZ", "price": result.unit_price})
        return cp_id

    def new_analysis_version(self, cp_id: int, description: str, unit: str, price_date: str, basis_text: str,
                             spec: AnalysisSpec) -> int:
        cp = self.get(cp_id)
        unit, description = _require(unit, "Birim"), _require(description, "Tarif")
        price_date = _price_date(price_date)
        basis_text = _require(basis_text, "Fiyat dayanağı / kaynak açıklaması")
        result, spec = self._analysis(spec, cp_id, cp.code)
        next_no = (cp.current.version_no if cp.current else 0) + 1
        with transaction(self.conn):
            repo.lock_versions(self.conn, cp_id)
            vid = repo.insert_analysis_version(self.conn, cp_id, next_no, description, unit, result.unit_price,
                                               price_date, basis_text)
            analyses.insert(self.conn, vid, spec)
            repo.refresh_search_text(self.conn, cp_id)
            audit.log(self.conn, "custom_position", cp_id, "GUNCELLE",
                      new={"version": next_no, "method": "ANALIZ", "price": result.unit_price},
                      note="Yeni analiz sürümü")
        return vid

    def analysis_of(self, version_id: int) -> AnalysisSpec | None:
        return analyses.get_spec(self.conn, version_id)

    def update_info(self, cp_id: int, name: str, category: str | None) -> None:
        cp = self.get(cp_id)
        name = _require(name, "İmalat adı")
        with transaction(self.conn):
            repo.update_info(self.conn, cp_id, name, _clean(category))
            audit.log(self.conn, "custom_position", cp_id, "GUNCELLE",
                      old={"name": cp.name, "category": cp.category}, new={"name": name, "category": category})

    def set_status(self, cp_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValidationError(f"Geçersiz durum: {status}")
        cp = self.get(cp_id)
        with transaction(self.conn):
            repo.set_status(self.conn, cp_id, status)
            audit.log(self.conn, "custom_position", cp_id, "GUNCELLE",
                      old={"status": cp.status}, new={"status": status})
