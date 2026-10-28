"""Sözleşme sürecinde yeni birim fiyat tespiti (YİGŞ md.22) ve değişmez tespit kayıtları.

- Hesap kuralları `domain.indexing` içindedir; bu servis endeks değerlerini veritabanından verir.
- Kayıt; girdiler, hesap adımları, kullanılan endeks dönemleri/değerleri ve kural kaynağı notuyla birlikte
  JSON olarak donmuş biçimde saklanır. Bütünlük özeti (sha256) her okumada doğrulanır; kayıt
  veritabanı tetikleyicisiyle değiştirilemez, düzeltme için yeni tespit yapılır.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, projects
from yapimmaliyet.domain.indexing import (
    SOURCE_NOTE,
    IndexRatio,
    ItemKind,
    NewPriceInput,
    NewPriceResult,
    OverheadMethod,
    new_unit_price,
)
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.index_service import IndexService
from yapimmaliyet.services.validators import clean, require, validate_price

PAYLOAD_SCHEMA = 1
PRICE_PLACES = 4   # unit_price sütunundaki kanonik metin basamağı


@dataclass(frozen=True)
class NewPriceRecord:
    id: int
    project_id: int | None
    code: str | None
    description: str
    unit: str | None
    unit_price: Decimal
    created_at: str


def _s(value) -> str | None:
    return None if value is None else str(value)


def _decimal(value, label: str, *, required: bool = False) -> Decimal | None:
    """Para ve oranlar yalnızca Decimal olarak kabul edilir (float sessiz hassasiyet kaybı yaratır)."""
    if value is None:
        if required:
            raise ValidationError(f"{label} girilmelidir.")
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError(f"{label} ondalık sayı (Decimal) olmalıdır: {value!r}")
    if value < 0:
        raise ValidationError(f"{label} negatif olamaz.")
    return value


def _check_input(inp: NewPriceInput) -> None:
    if not isinstance(inp, NewPriceInput):
        raise ValidationError("Geçersiz fiyat tespit girdisi.")
    if not isinstance(inp.kind, ItemKind) or not isinstance(inp.overhead, OverheadMethod):
        raise ValidationError("İmalat türü ya da kâr ve genel gider yöntemi geçersiz.")
    if inp.kind is ItemKind.OZEL_IMALAT and inp.market_price is not None:
        validate_price(_decimal(inp.market_price, "Özel imalat birim fiyatı"))
    _decimal(inp.contract_rate, "Sözleşmedeki kâr ve genel gider oranı")
    _decimal(inp.similar_ratio, "Benzer poz kırım oranı")
    for c in inp.components:
        require(c.description, "Bileşen açıklaması")
        _decimal(c.amount, f"'{c.description}' karsız tutarı", required=True)


def _ratio_payload(r: IndexRatio) -> dict:
    return {"series": r.series, "numerator_period": r.numerator_period, "numerator": _s(r.numerator),
            "denominator_period": r.denominator_period, "denominator": _s(r.denominator), "factor": _s(r.factor)}


def _input_payload(inp: NewPriceInput) -> dict:
    return {
        "kind": inp.kind.value, "kind_label": inp.kind.label, "price_difference": inp.price_difference,
        "tender_date": inp.tender_date or None, "approval_date": inp.approval_date,
        "determination_date": inp.determination_date, "market_price": _s(inp.market_price),
        "series": inp.series, "overhead": inp.overhead.value, "overhead_label": inp.overhead.label,
        "contract_rate": _s(inp.contract_rate), "similar_ratio": _s(inp.similar_ratio),
        "price_places": inp.price_places,
        "components": [{"description": c.description, "amount": _s(c.amount), "published": c.published,
                        "series": c.series} for c in inp.components],
    }


class NewPriceService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- hesap -------------------------------------------------------------
    def calculate(self, inp: NewPriceInput) -> NewPriceResult:
        """Taslak hesap: veritabanına yazılmaz. Eksik endeks ve kural hataları ValidationError olur."""
        _check_input(inp)
        try:
            return new_unit_price(inp, IndexService(self.conn).lookup())
        except (ValueError, TypeError) as exc:   # MissingIndexError da ValueError'dır
            raise ValidationError(str(exc)) from exc

    # --- kayıt -------------------------------------------------------------
    def save(self, inp: NewPriceInput, description: str, code: str | None = None, unit: str | None = None,
             project_id: int | None = None) -> int:
        """Hesabı yeniden yapar ve değişmez tespit kaydı olarak saklar; kaydın kimliğini döndürür."""
        description = require(description, "Tanım")
        code, unit = clean(code), clean(unit)
        if project_id is not None and projects.get_project(self.conn, project_id) is None:
            raise NotFoundError("Proje bulunamadı.")
        result = self.calculate(inp)
        price_text = to_db_text(validate_price(result.unit_price), PRICE_PLACES)
        payload = {
            "schema": PAYLOAD_SCHEMA,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": __version__,
            "source_note": SOURCE_NOTE,
            "record": {"project_id": project_id, "code": code, "description": description, "unit": unit},
            "input": _input_payload(inp),
            "result": {"unit_price": _s(result.unit_price), "steps": list(result.steps),
                       "ratios": [_ratio_payload(r) for r in result.ratios]},
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with transaction(self.conn):
            rid = self.conn.execute(
                "INSERT INTO new_price_records(project_id, code, description, unit, unit_price, payload_json, sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, code, description, unit, price_text, text, sha)).lastrowid
            audit.log(self.conn, "new_price_record", rid, "EKLE",
                      new={"code": code, "description": description, "unit_price": price_text,
                           "kind": inp.kind.value}, note="Yeni birim fiyat tespit edildi")
        return rid

    def records(self, project_id: int | None = None) -> list[NewPriceRecord]:
        sql = "SELECT id, project_id, code, description, unit, unit_price, created_at FROM new_price_records"
        params: tuple = ()
        if project_id is not None:
            sql, params = sql + " WHERE project_id = ?", (project_id,)
        rows = self.conn.execute(sql + " ORDER BY created_at DESC, id DESC", params)
        return [NewPriceRecord(r["id"], r["project_id"], r["code"], r["description"], r["unit"],
                               from_db_text(r["unit_price"]), r["created_at"]) for r in rows]

    def payload(self, record_id: int) -> dict:
        row = self.conn.execute("SELECT payload_json, sha256 FROM new_price_records WHERE id = ?",
                                (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("Fiyat tespit kaydı bulunamadı.")
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["sha256"]:
            raise ConflictError("Fiyat tespit kaydının bütünlük özeti uyuşmuyor; kayıt bozulmuş olabilir.")
        return json.loads(row["payload_json"])
