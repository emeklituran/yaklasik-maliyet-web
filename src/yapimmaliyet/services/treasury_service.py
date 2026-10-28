"""Kasa ve banka hesapları: hesap kartları, giriş/çıkış hareketleri ve hesaplar arası virman.

Bakiye saklanmaz: açılış bakiyesi + hareketlerin toplamı. Cari tahsilat/ödeme, çek/senet tahsili ve kredi
ödemeleri hareketlerini kendileri oluşturur (source); bu hareketler yalnız kendi sayfalarından silinir.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require, validate_iso_date

KINDS = {"KASA": "Kasa", "BANKA": "Banka"}
SOURCES = {"ELLE": "Gelir / gider", "CARI": "Cari hesap", "VIRMAN": "Virman", "SENET": "Çek / senet",
           "BORC": "Kredi / borç ödemesi"}
ZERO = Decimal(0)


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    kind: str
    bank: str
    iban: str
    opening: Decimal
    active: bool
    balance: Decimal

    @property
    def label(self) -> str:
        return f"{self.name} ({KINDS[self.kind]})"


@dataclass(frozen=True)
class Move:
    id: int
    account_id: int
    move_date: str
    amount: Decimal
    description: str
    source: str
    project_id: int | None
    balance: Decimal = ZERO     # hareket sonrası bakiye

    @property
    def source_label(self) -> str:
        return SOURCES[self.source]


def money(value, field: str, *, positive: bool = False, allow_negative: bool = False) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} geçerli bir tutar değil.") from exc
    if positive and d <= 0:
        raise ValidationError(f"{field} sıfırdan büyük olmalıdır.")
    if not allow_negative and d < 0:
        raise ValidationError(f"{field} negatif olamaz.")
    return d


def insert_move(conn: sqlite3.Connection, account_id: int, date: str, amount: Decimal, description: str,
                source: str, project_id: int | None = None, group_key: str | None = None) -> int:
    """Hareket ekler (çağıran transaction açmış olmalı)."""
    if conn.execute("SELECT 1 FROM fin_accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise NotFoundError("Kasa / banka hesabı bulunamadı.")
    cur = conn.execute("INSERT INTO fin_moves(account_id, move_date, amount, description, source, group_key, "
                       "project_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (account_id, date, to_db_text(amount, 2), require(description, "Açıklama"), source,
                        group_key, project_id))
    return cur.lastrowid


class TreasuryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- hesaplar ----------------------------------------------------------------
    def accounts(self, include_inactive: bool = False) -> list[Account]:
        rows = self.conn.execute(
            "SELECT a.* FROM fin_accounts a" +("" if include_inactive else " WHERE a.active = 1") +
            " ORDER BY a.kind DESC, a.name").fetchall()
        out = []
        for r in rows:
            moves = sum((from_db_text(m["amount"]) for m in self.conn.execute(
                "SELECT amount FROM fin_moves WHERE account_id = ?", (r["id"],))), ZERO)
            opening = from_db_text(r["opening"])
            out.append(Account(r["id"], r["name"], r["kind"], r["bank"], r["iban"], opening, bool(r["active"]),
                               opening + moves))
        return out

    def account(self, account_id: int) -> Account:
        acc = next((a for a in self.accounts(True) if a.id == account_id), None)
        if acc is None:
            raise NotFoundError("Kasa / banka hesabı bulunamadı.")
        return acc

    def total_cash(self) -> Decimal:
        return sum((a.balance for a in self.accounts()), ZERO)

    def _clean(self, name, kind, opening):
        if kind not in KINDS:
            raise ValidationError("Hesap türü Kasa ya da Banka olmalıdır.")
        return require(name, "Hesap adı"), money(opening, "Açılış bakiyesi", allow_negative=True)

    def create_account(self, name: str, kind: str, bank: str = "", iban: str = "", opening="0") -> int:
        name, opening = self._clean(name, kind, opening)
        with transaction(self.conn):
            cur = self.conn.execute("INSERT INTO fin_accounts(name, kind, bank, iban, opening) VALUES (?, ?, ?, ?, ?)",
                                    (name, kind, clean(bank) or "", (clean(iban) or "").replace(" ", "").upper(),
                                     to_db_text(opening, 2)))
            audit.log(self.conn, "fin_account", cur.lastrowid, "EKLE", new={"name": name, "kind": kind})
        return cur.lastrowid

    def update_account(self, account_id: int, name: str, kind: str, bank: str, iban: str, opening) -> None:
        self.account(account_id)
        name, opening = self._clean(name, kind, opening)
        with transaction(self.conn):
            self.conn.execute("UPDATE fin_accounts SET name=?, kind=?, bank=?, iban=?, opening=? WHERE id=?",
                              (name, kind, clean(bank) or "", (clean(iban) or "").replace(" ", "").upper(),
                               to_db_text(opening, 2), account_id))
            audit.log(self.conn, "fin_account", account_id, "GUNCELLE", new={"name": name})

    def set_active(self, account_id: int, active: bool) -> None:
        self.account(account_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE fin_accounts SET active = ? WHERE id = ?", (int(active), account_id))

    def delete_account(self, account_id: int) -> None:
        acc = self.account(account_id)
        if self.conn.execute("SELECT 1 FROM fin_moves WHERE account_id = ?", (account_id,)).fetchone():
            raise ConflictError("Hesapta hareket var; silinemez. Listede görünmemesi için pasifleştirebilirsiniz.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM fin_accounts WHERE id = ?", (account_id,))
            audit.log(self.conn, "fin_account", account_id, "SIL", old={"name": acc.name})

    # --- hareketler --------------------------------------------------------------
    def moves(self, account_id: int) -> list[Move]:
        acc = self.account(account_id)
        bal = acc.opening
        out = []
        for r in self.conn.execute("SELECT * FROM fin_moves WHERE account_id = ? ORDER BY move_date, id",
                                   (account_id,)):
            amount = from_db_text(r["amount"])
            bal += amount
            out.append(Move(r["id"], r["account_id"], r["move_date"], amount, r["description"], r["source"],
                            r["project_id"], bal))
        return out

    def add_move(self, account_id: int, date: str, amount, description: str, project_id: int | None = None) -> int:
        """Elle gelir (+) ya da gider (−) kaydı."""
        amount = money(amount, "Tutar", allow_negative=True)
        if amount == 0:
            raise ValidationError("Tutar sıfır olamaz.")
        date = validate_iso_date(date, "Tarih", required=True)
        with transaction(self.conn):
            mid = insert_move(self.conn, account_id, date, amount, description, "ELLE", project_id)
            audit.log(self.conn, "fin_move", mid, "EKLE", new={"amount": str(amount), "description": description})
        return mid

    def transfer(self, from_id: int, to_id: int, date: str, amount, description: str = "Virman") -> str:
        if from_id == to_id:
            raise ValidationError("Virman için iki farklı hesap seçin.")
        amount = money(amount, "Tutar", positive=True)
        date = validate_iso_date(date, "Tarih", required=True)
        src, dst = self.account(from_id), self.account(to_id)
        key = uuid.uuid4().hex
        text = clean(description) or "Virman"
        with transaction(self.conn):
            insert_move(self.conn, from_id, date, -amount, f"{text} → {dst.name}", "VIRMAN", group_key=key)
            insert_move(self.conn, to_id, date, amount, f"{text} ← {src.name}", "VIRMAN", group_key=key)
            audit.log(self.conn, "fin_move", None, "EKLE", new={"virman": str(amount), "from": src.name,
                                                                "to": dst.name})
        return key

    def delete_move(self, move_id: int) -> None:
        r = self.conn.execute("SELECT * FROM fin_moves WHERE id = ?", (move_id,)).fetchone()
        if r is None:
            raise NotFoundError("Hareket bulunamadı.")
        if r["source"] not in ("ELLE", "VIRMAN"):
            raise ConflictError(f"Bu hareket {SOURCES[r['source']].lower()} kaydından geliyor; o kaydı kendi "
                                "sayfasından silin.")
        with transaction(self.conn):
            if r["source"] == "VIRMAN" and r["group_key"]:
                self.conn.execute("DELETE FROM fin_moves WHERE group_key = ?", (r["group_key"],))
            else:
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (move_id,))
            audit.log(self.conn, "fin_move", move_id, "SIL", old={"amount": r["amount"],
                                                                  "description": r["description"]})
