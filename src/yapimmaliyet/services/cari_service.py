"""Cari hesaplar: firma (taraf) bazında borç/alacak hareketleri, ekstre, açık kalemler ve vade takibi.

Cari kart, Firmalar ve Taraflar kaydıdır. Hareket türleri:
- Borç dekontu / satış faturası (BORC): cari bize borçlanır.     - Alacak dekontu / alış faturası (ALACAK)
- Tahsilat (TAHSILAT): cariden para aldık (kasa/bankaya giriş). - Ödeme (ODEME): cariye para verdik (çıkış)
Bakiye = (BORC + ODEME) − (ALACAK + TAHSILAT); artı ise cari borçlu, eksi ise alacaklıdır.
Açık kalem ve gecikme: karşı yöndeki hareketler vadesi en erken olan belgeden başlayarak kapatılır (FIFO).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.treasury_service import insert_move, money
from yapimmaliyet.services.validators import clean, require, validate_iso_date

KINDS = {"BORC": "Borç dekontu / satış faturası", "ALACAK": "Alacak dekontu / alış faturası",
         "TAHSILAT": "Tahsilat", "ODEME": "Ödeme"}
SHORT = {"BORC": "Borç", "ALACAK": "Alacak", "TAHSILAT": "Tahsilat", "ODEME": "Ödeme"}
DEBIT = ("BORC", "ODEME")
ZERO = Decimal(0)


@dataclass(frozen=True)
class CariEntry:
    id: int
    party_id: int
    project_id: int | None
    entry_date: str
    kind: str
    description: str
    document_no: str
    amount: Decimal
    due_date: str | None
    balance: Decimal = ZERO      # ekstrede hareket sonrası bakiye

    @property
    def debit(self) -> Decimal:
        return self.amount if self.kind in DEBIT else ZERO

    @property
    def credit(self) -> Decimal:
        return ZERO if self.kind in DEBIT else self.amount


@dataclass(frozen=True)
class CariSummary:
    party_id: int
    name: str
    debit: Decimal
    credit: Decimal
    overdue: Decimal
    last_date: str | None
    count: int

    @property
    def balance(self) -> Decimal:
        return self.debit - self.credit

    @property
    def state(self) -> str:
        return "Borçlu" if self.balance > 0 else "Alacaklı" if self.balance < 0 else "Kapalı"


@dataclass(frozen=True)
class OpenItem:
    entry: CariEntry
    party_name: str
    remaining: Decimal
    receivable: bool            # True: bizim alacağımız (cari borçlu)

    @property
    def due(self) -> str:
        return self.entry.due_date or self.entry.entry_date


def _entry(r) -> CariEntry:
    return CariEntry(r["id"], r["party_id"], r["project_id"], r["entry_date"], r["kind"], r["description"],
                     r["document_no"], from_db_text(r["amount"]), r["due_date"])


def open_items_of(entries: list[CariEntry]) -> list[tuple[CariEntry, Decimal, bool]]:
    """FIFO eşleştirme: bakiye yönündeki belgelerin kapanmamış kısımları (en eski belge önce kapanır).

    Açık tutar belgelerin toplamını aşarsa (ör. avans ödemesi) aşan kısım vadesiz sayılır ve listelenmez.
    """
    debit = sum((e.debit for e in entries), ZERO)
    credit = sum((e.credit for e in entries), ZERO)
    if debit == credit:
        return []
    receivable = debit > credit
    docs = sorted((e for e in entries if e.kind == ("BORC" if receivable else "ALACAK")),
                  key=lambda e: (e.due_date or e.entry_date, e.id))
    left = abs(debit - credit)
    out = []
    for e in reversed(docs):          # vadesi en geç olan belgeler açık kalır
        if left <= 0:
            break
        part = min(e.amount, left)
        out.append((e, part, receivable))
        left -= part
    return out[::-1]


class CariService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _party_name(self, party_id: int) -> str:
        r = self.conn.execute("SELECT name FROM parties WHERE id = ?", (party_id,)).fetchone()
        if r is None:
            raise NotFoundError("Cari (firma) bulunamadı.")
        return r["name"]

    def entries(self, party_id: int) -> list[CariEntry]:
        rows = self.conn.execute("SELECT * FROM cari_entries WHERE party_id = ? ORDER BY entry_date, id",
                                 (party_id,)).fetchall()
        return [_entry(r) for r in rows]

    def statement(self, party_id: int) -> list[CariEntry]:
        self._party_name(party_id)
        bal, out = ZERO, []
        for e in self.entries(party_id):
            bal += e.debit - e.credit
            out.append(CariEntry(**{**e.__dict__, "balance": bal}))
        return out

    def summaries(self, include_empty: bool = True, today: str | None = None) -> list[CariSummary]:
        today = today or date.today().isoformat()
        parties = self.conn.execute("SELECT id, name, active FROM parties ORDER BY name").fetchall()
        by_party: dict[int, list[CariEntry]] = {}
        for r in self.conn.execute("SELECT * FROM cari_entries ORDER BY entry_date, id"):
            by_party.setdefault(r["party_id"], []).append(_entry(r))
        out = []
        for p in parties:
            es = by_party.get(p["id"], [])
            if not es and (not include_empty or not p["active"]):
                continue
            overdue = sum((part for e, part, rec in open_items_of(es) if (e.due_date or e.entry_date) < today),
                          ZERO)
            out.append(CariSummary(p["id"], p["name"], sum((e.debit for e in es), ZERO),
                                   sum((e.credit for e in es), ZERO), overdue,
                                   es[-1].entry_date if es else None, len(es)))
        return out

    def open_items(self, party_id: int | None = None) -> list[OpenItem]:
        names = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM parties")}
        ids = [party_id] if party_id else [r["party_id"] for r in self.conn.execute(
            "SELECT DISTINCT party_id FROM cari_entries")]
        out = []
        for pid in ids:
            for e, part, rec in open_items_of(self.entries(pid)):
                out.append(OpenItem(e, names.get(pid, "?"), part, rec))
        return sorted(out, key=lambda x: x.due)

    def totals(self, today: str | None = None) -> dict[str, Decimal]:
        rows = self.summaries(include_empty=False, today=today)
        recv = sum((s.balance for s in rows if s.balance > 0), ZERO)
        pay = -sum((s.balance for s in rows if s.balance < 0), ZERO)
        today = today or date.today().isoformat()
        items = self.open_items()
        return {"receivable": recv, "payable": pay, "net": recv - pay,
                "overdue_receivable": sum((i.remaining for i in items if i.receivable and i.due < today), ZERO),
                "overdue_payable": sum((i.remaining for i in items if not i.receivable and i.due < today), ZERO)}

    def add_entry(self, party_id: int, kind: str, entry_date: str, amount, description: str,
                  document_no: str = "", due_date: str | None = None, project_id: int | None = None,
                  account_id: int | None = None) -> int:
        name = self._party_name(party_id)
        if kind not in KINDS:
            raise ValidationError("Geçersiz hareket türü.")
        amount = money(amount, "Tutar", positive=True)
        entry_date = validate_iso_date(entry_date, "Tarih", required=True)
        due_date = validate_iso_date(due_date, "Vade tarihi")
        if due_date and due_date < entry_date:
            raise ValidationError("Vade tarihi belge tarihinden önce olamaz.")
        description = require(description, "Açıklama")
        if kind in ("TAHSILAT", "ODEME") and due_date:
            due_date = None
        with transaction(self.conn):
            move_id = None
            if account_id and kind in ("TAHSILAT", "ODEME"):
                move_id = insert_move(self.conn, account_id, entry_date, amount if kind == "TAHSILAT" else -amount,
                                      f"{SHORT[kind]}: {name} · {description}", "CARI", project_id)
            cur = self.conn.execute(
                "INSERT INTO cari_entries(party_id, project_id, entry_date, kind, description, document_no, amount,"
                " due_date, move_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (party_id, project_id, entry_date, kind, description, clean(document_no) or "",
                 to_db_text(amount, 2), due_date, move_id))
            audit.log(self.conn, "cari_entry", cur.lastrowid, "EKLE",
                      new={"party": name, "kind": kind, "amount": str(amount)})
        return cur.lastrowid

    def delete_entry(self, entry_id: int) -> None:
        r = self.conn.execute("SELECT * FROM cari_entries WHERE id = ?", (entry_id,)).fetchone()
        if r is None:
            raise NotFoundError("Cari hareket bulunamadı.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM cari_entries WHERE id = ?", (entry_id,))
            if r["move_id"]:
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (r["move_id"],))
            audit.log(self.conn, "cari_entry", entry_id, "SIL", old={"kind": r["kind"], "amount": r["amount"],
                                                                     "description": r["description"]})
