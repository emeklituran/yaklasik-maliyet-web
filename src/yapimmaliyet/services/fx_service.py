"""Döviz kurları: TCMB bülteninden çekme, elle giriş, keşfe kur sabitleme ve TL → döviz karşılığı.

TL temel para birimidir. Kur çekimi başarısız olursa hata bildirilir; varsayılan ya da tahmini kurla hesap
yapılmaz. Kayıtlar değiştirilemez (veritabanı tetikleyicisi); bir keşfe sabitlenen kur yeni çekimlerden etkilenmez.
"""

from __future__ import annotations

import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.fileio import tcmb_xml
from yapimmaliyet.infra.errors import AppError, ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, validate_iso_date

RATE_TYPES = {"DOVIZ_ALIS": "Döviz alış", "DOVIZ_SATIS": "Döviz satış", "EFEKTIF_ALIS": "Efektif alış",
              "EFEKTIF_SATIS": "Efektif satış", "MANUEL": "Elle girilen"}
SOURCES = {"TCMB": "TCMB gösterge kuru", "MANUEL": "Elle giriş"}
RATE_PLACES = 6
TIMEOUT_S = 10


@dataclass(frozen=True)
class FxRate:
    id: int
    currency: str
    rate: Decimal            # 1 birim döviz = rate TL
    raw_rate: Decimal | None
    unit: int
    rate_type: str
    rate_date: str
    source: str
    source_url: str | None
    bulletin_no: str | None
    fetched_at: str
    note: str | None

    @property
    def label(self) -> str:
        return (f"{self.currency} {RATE_TYPES[self.rate_type]} {self.rate} TL · {self.rate_date} · "
                f"{SOURCES[self.source]}")


def _row(r: sqlite3.Row) -> FxRate:
    return FxRate(r["id"], r["currency"], from_db_text(r["rate"]), from_db_text(r["raw_rate"]), r["unit"],
                  r["rate_type"], r["rate_date"], r["source"], r["source_url"], r["bulletin_no"], r["fetched_at"],
                  r["note"])


@dataclass(frozen=True)
class FetchResult:
    bulletin_date: str
    bulletin_no: str
    added: int
    already: int
    url: str


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "YapimMaliyet"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:   # noqa: S310 (sabit https adresi)
        return resp.read(2_000_000)


class FxService:
    def __init__(self, conn: sqlite3.Connection, downloader: Callable[[str], bytes] = _download):
        self.conn = conn
        self.downloader = downloader

    # --- kayıtlar --------------------------------------------------------------
    def list_rates(self, currency: str | None = None, limit: int = 500) -> list[FxRate]:
        sql, args = "SELECT * FROM exchange_rates", []
        if currency:
            sql += " WHERE currency = ?"
            args.append(currency.upper())
        sql += " ORDER BY rate_date DESC, fetched_at DESC, currency, rate_type LIMIT ?"
        return [_row(r) for r in self.conn.execute(sql, (*args, limit))]

    def latest(self) -> list[FxRate]:
        """Her döviz ve kur türü için en son tarihli kayıt (kaynağıyla)."""
        return [_row(r) for r in self.conn.execute(
            "SELECT * FROM exchange_rates e WHERE NOT EXISTS (SELECT 1 FROM exchange_rates x"
            " WHERE x.currency = e.currency AND x.rate_type = e.rate_type"
            " AND (x.rate_date > e.rate_date OR (x.rate_date = e.rate_date AND x.id > e.id)))"
            " ORDER BY e.currency, e.rate_type")]

    def get(self, rate_id: int) -> FxRate:
        r = self.conn.execute("SELECT * FROM exchange_rates WHERE id = ?", (rate_id,)).fetchone()
        if r is None:
            raise NotFoundError("Kur kaydı bulunamadı.")
        return _row(r)

    def currencies(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT currency FROM exchange_rates ORDER BY currency")]

    # --- TCMB ----------------------------------------------------------------
    def fetch_tcmb(self, day: str | None = None) -> FetchResult:
        """TCMB bültenini indirir ve kaydeder (aynı bülten ikinci kez eklenmez). Ağ yoksa AppError."""
        url = tcmb_xml.archive_url(validate_iso_date(day, "Kur tarihi")) if day else tcmb_xml.TODAY_URL
        try:
            content = self.downloader(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AppError("TCMB kur bültenine ulaşılamadı. İnternet bağlantısını kontrol edin; son kayıtlı "
                           "kurlar kullanılabilir ya da kuru elle girebilirsiniz.", detail=f"{url}: {exc}") from exc
        try:
            bulletin = tcmb_xml.parse(content)
        except tcmb_xml.TcmbFormatError as exc:
            raise AppError(f"TCMB kur bülteni işlenemedi: {exc}", detail=url) from exc
        added = 0
        with transaction(self.conn):
            for r in bulletin.rates:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO exchange_rates(currency, rate, raw_rate, unit, rate_type, rate_date, source,"
                    " source_url, bulletin_no) VALUES (?, ?, ?, ?, ?, ?, 'TCMB', ?, ?)",
                    (r.currency, to_db_text(r.per_unit, RATE_PLACES), to_db_text(r.raw, RATE_PLACES), r.unit,
                     r.rate_type, bulletin.date, url, bulletin.bulletin_no))
                added += cur.rowcount
            audit.log(self.conn, "exchange_rate", None, "TOPLU",
                      new={"source": "TCMB", "date": bulletin.date, "added": added}, note=url)
        return FetchResult(bulletin.date, bulletin.bulletin_no, added, len(bulletin.rates) - added, url)

    # --- elle giriş ------------------------------------------------------------
    def add_manual(self, currency: str, rate, rate_date: str, note: str | None = None) -> int:
        code = (currency or "").strip().upper()
        if len(code) != 3 or not code.isalpha() or code == "TRY":
            raise ValidationError("Döviz kodu üç harfli olmalıdır (ör. USD, EUR); TRY temel para birimidir.")
        try:
            value = Decimal(str(rate))
        except ArithmeticError as exc:
            raise ValidationError(f"Geçersiz kur: {rate}") from exc
        if not value.is_finite() or value <= 0:
            raise ValidationError("Kur sıfırdan büyük olmalıdır.")
        day = validate_iso_date(rate_date, "Kur tarihi", required=True)
        try:
            with transaction(self.conn):
                rid = self.conn.execute(
                    "INSERT INTO exchange_rates(currency, rate, raw_rate, unit, rate_type, rate_date, source, note)"
                    " VALUES (?, ?, ?, 1, 'MANUEL', ?, 'MANUEL', ?)",
                    (code, to_db_text(value, RATE_PLACES), to_db_text(value, RATE_PLACES), day, clean(note))).lastrowid
                audit.log(self.conn, "exchange_rate", rid, "EKLE", new={"currency": code, "rate": value, "date": day},
                          note="Elle girilen kur")
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"{code} için {day} tarihli elle girilmiş kur zaten var.") from exc
        return rid

    # --- keşfe sabitleme ve dönüşüm ---------------------------------------------
    def pin_to_estimate(self, estimate_id: int, rate_id: int | None) -> None:
        """Keşfin döviz karşılığı için kullanılacak kuru sabitler (None: kaldır)."""
        est = self.conn.execute("SELECT id, fx_rate_id, locked FROM estimates WHERE id = ?", (estimate_id,)).fetchone()
        if est is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        if est["locked"]:
            raise ConflictError("Keşif kilitli; kur değiştirmek için önce kilidi kaldırın.")
        if rate_id is not None:
            self.get(rate_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE estimates SET fx_rate_id = ? WHERE id = ?", (rate_id, estimate_id))
            audit.log(self.conn, "estimate", estimate_id, "GUNCELLE", old={"fx_rate_id": est["fx_rate_id"]},
                      new={"fx_rate_id": rate_id})

    def estimate_rate(self, estimate_id: int) -> FxRate | None:
        r = self.conn.execute("SELECT fx_rate_id FROM estimates WHERE id = ?", (estimate_id,)).fetchone()
        return self.get(r[0]) if r and r[0] is not None else None

    @staticmethod
    def convert(amount_tl: Decimal, rate: FxRate) -> Decimal:
        return quantize(amount_tl / rate.rate, 2)
