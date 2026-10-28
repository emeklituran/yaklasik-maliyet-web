"""Oran ve parametre tablosu: mevzuat / fiyat farkı / endeks oranları kullanıcı girişidir.

Kurallar: program hiçbir oranı kendiliğinden üretmez ya da önceden yüklemez. Her oran anahtar, değer, geçerlilik aralığı ve
KAYNAK (kanun/tebliğ/sayı) ile girilir; aynı anahtarın geçerlilik aralıkları çakışamaz. Doğrulama (verified) kullanıcı tarafından
işaretlenir; doğrulanmamış, süresi dolmuş ya da yarın bitecek oranlar `warnings` ile bildirilir. Oran değiştirmek yeni kayıt
açmaktır (eski kayıt geçmişte kalır); doğrulanmış kaydın değeri düzenlenemez.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require, validate_iso_date

KINDS = {"MEVZUAT": "Mevzuat oranı", "FIYAT_FARKI": "Fiyat farkı parametresi", "ENDEKS": "Endeks", "DIGER": "Diğer"}


@dataclass(frozen=True)
class Rate:
    id: int
    key: str
    name: str
    kind: str
    value: Decimal
    unit: str
    valid_from: str
    valid_to: str | None
    source_ref: str
    source_date: str | None
    verified: bool
    verified_by: str | None
    notes: str


def _rate(r) -> Rate:
    return Rate(r["id"], r["key"], r["name"], r["kind"], from_db_text(r["value"]), r["unit"], r["valid_from"], r["valid_to"],
                r["source_ref"], r["source_date"], bool(r["verified"]), r["verified_by"], r["notes"])


class RateService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def rates(self, key: str | None = None) -> list[Rate]:
        sql = "SELECT * FROM ref_rates" + (" WHERE key = ?" if key else "") + " ORDER BY key, valid_from DESC, id DESC"
        return [_rate(r) for r in self.conn.execute(sql, (key,) if key else ())]

    def get(self, rate_id: int) -> Rate:
        r = self.conn.execute("SELECT * FROM ref_rates WHERE id = ?", (rate_id,)).fetchone()
        if r is None:
            raise NotFoundError("Oran kaydı bulunamadı.")
        return _rate(r)

    def active_on(self, key: str, on: str | None = None) -> Rate | None:
        """Tarihte geçerli oran; yoksa None (program varsayılan oran uydurmaz)."""
        on = on or date.today().isoformat()
        r = self.conn.execute("SELECT * FROM ref_rates WHERE key = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)"
                              " ORDER BY valid_from DESC LIMIT 1", (key, on, on)).fetchone()
        return _rate(r) if r else None

    def _values(self, d: dict, exclude_id: int | None = None) -> dict:
        if d.get("kind") not in KINDS:
            raise ValidationError("Oran türü seçilmelidir.")
        try:
            value = to_decimal(d.get("value"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Oran değeri geçerli bir sayı değil.") from exc
        vf = validate_iso_date(d.get("valid_from"), "Geçerlilik başlangıcı", required=True)
        vt = validate_iso_date(d.get("valid_to"), "Geçerlilik bitişi")
        if vt and vt < vf:
            raise ValidationError("Geçerlilik bitişi başlangıçtan önce olamaz.")
        key = require(d.get("key"), "Oran anahtarı").upper().replace(" ", "_")
        for o in self.rates(key):
            if o.id == exclude_id:
                continue
            if vf <= (o.valid_to or "9999-12-31") and (vt or "9999-12-31") >= o.valid_from:
                raise ConflictError(f"'{key}' için geçerlilik aralığı mevcut kayıtla ({o.valid_from} – {o.valid_to or 'süresiz'}) "
                                    "çakışıyor; önce eski kaydın bitiş tarihini girin.")
        return dict(key=key, name=require(d.get("name"), "Oran adı"), kind=d["kind"], value=to_db_text(value, 6),
                    unit=clean(d.get("unit")) or "%", valid_from=vf, valid_to=vt,
                    source_ref=require(d.get("source_ref"), "Kaynak (kanun / tebliğ / sayı)"),
                    source_date=validate_iso_date(d.get("source_date"), "Kaynak tarihi"), notes=clean(d.get("notes")) or "")

    def add(self, data: dict) -> int:
        v = self._values(data)
        cols = list(v)
        with transaction(self.conn):
            rid = self.conn.execute(f"INSERT INTO ref_rates({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    list(v.values())).lastrowid
            audit.log(self.conn, "ref_rate", rid, "EKLE", new={"key": v["key"], "value": v["value"], "source": v["source_ref"]})
        return rid

    def update(self, rate_id: int, data: dict) -> None:
        old = self.get(rate_id)
        v = self._values(data, exclude_id=rate_id)
        if old.verified and (to_db_text(old.value, 6) != v["value"] or old.valid_from != v["valid_from"]):
            raise ConflictError("Doğrulanmış oranın değeri ve başlangıç tarihi değiştirilemez; yeni kayıt açın.")
        with transaction(self.conn):
            self.conn.execute(f"UPDATE ref_rates SET {', '.join(k + ' = ?' for k in v)} WHERE id = ?", [*v.values(), rate_id])
            audit.log(self.conn, "ref_rate", rate_id, "GUNCELLE", old={"value": str(old.value)}, new={"value": v["value"]})

    def verify(self, rate_id: int, user: str) -> None:
        self.get(rate_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE ref_rates SET verified = 1, verified_by = ?, verified_at = strftime('%Y-%m-%dT%H:%M:%S',"
                              " 'now', 'localtime') WHERE id = ?", (require(user, "Doğrulayan"), rate_id))
            audit.log(self.conn, "ref_rate", rate_id, "GUNCELLE", new={"verified": True, "by": user})

    def delete(self, rate_id: int) -> None:
        r = self.get(rate_id)
        if r.verified:
            raise ConflictError("Doğrulanmış oran silinemez; bitiş tarihi girerek geçmişte bırakın.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM ref_rates WHERE id = ?", (rate_id,))
            audit.log(self.conn, "ref_rate", rate_id, "SIL", old={"key": r.key, "value": str(r.value)})

    def warnings(self, today: str | None = None) -> list[str]:
        today = today or date.today().isoformat()
        out = []
        for r in self.rates():
            live = r.valid_from <= today and (r.valid_to is None or r.valid_to >= today)
            if live and not r.verified:
                out.append(f"{r.name} ({r.key}) doğrulanmamış: kaynak '{r.source_ref}' kontrol edilip doğrulanmalı.")
            if r.valid_to and r.valid_to < today and not self.active_on(r.key, today):
                out.append(f"{r.name} ({r.key}) için {r.valid_to} sonrası geçerli oran girilmemiş.")
        return out
