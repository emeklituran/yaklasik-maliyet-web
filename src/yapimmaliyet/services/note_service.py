"""Çek ve senetler: alınan (müşteri) ve verilen (firma) çek/senet portföyü, vade ve durum takibi.

Durumlar: Portföyde → Tahsil edildi (alınan; hesaba giriş) / Ödendi (verilen; hesaptan çıkış) / Ciro edildi /
Karşılıksız / İade. Tahsil ya da ödeme hesabı seçilirse kasa/banka hareketi oluşur; durum geri alınınca silinir.
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
from yapimmaliyet.services.validators import clean, validate_iso_date

DIRECTIONS = {"ALINAN": "Alınan", "VERILEN": "Verilen"}
KINDS = {"CEK": "Çek", "SENET": "Senet"}
STATUSES = {"PORTFOY": "Portföyde", "TAHSIL": "Tahsil edildi", "ODENDI": "Ödendi", "CIRO": "Ciro edildi",
            "KARSILIKSIZ": "Karşılıksız", "IADE": "İade edildi"}
OPEN = ("PORTFOY",)
ZERO = Decimal(0)


@dataclass(frozen=True)
class Note:
    id: int
    direction: str
    kind: str
    party_id: int | None
    party_name: str
    number: str
    bank: str
    amount: Decimal
    issue_date: str
    due_date: str
    status: str
    notes: str

    @property
    def label(self) -> str:
        return f"{DIRECTIONS[self.direction]} {KINDS[self.kind].lower()}"

    def days_left(self, today: str | None = None) -> int:
        return (date.fromisoformat(self.due_date) - date.fromisoformat(today or date.today().isoformat())).days


class NoteService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def notes(self, direction: str | None = None, open_only: bool = False) -> list[Note]:
        sql = ("SELECT n.*, COALESCE(p.name, '') AS party_name FROM fin_notes n "
               "LEFT JOIN parties p ON p.id = n.party_id WHERE 1=1")
        args: list = []
        if direction:
            sql += " AND n.direction = ?"
            args.append(direction)
        if open_only:
            sql += " AND n.status = 'PORTFOY'"
        rows = self.conn.execute(sql + " ORDER BY n.due_date, n.id", args).fetchall()
        return [Note(r["id"], r["direction"], r["kind"], r["party_id"], r["party_name"], r["number"], r["bank"],
                     from_db_text(r["amount"]), r["issue_date"], r["due_date"], r["status"], r["notes"])
                for r in rows]

    def note(self, note_id: int) -> Note:
        n = next((x for x in self.notes() if x.id == note_id), None)
        if n is None:
            raise NotFoundError("Çek / senet bulunamadı.")
        return n

    def totals(self, today: str | None = None) -> dict[str, Decimal]:
        today = today or date.today().isoformat()
        out = {"in": ZERO, "out": ZERO, "overdue_in": ZERO, "overdue_out": ZERO}
        for n in self.notes(open_only=True):
            key = "in" if n.direction == "ALINAN" else "out"
            out[key] += n.amount
            if n.due_date < today:
                out["overdue_" + key] += n.amount
        return out

    def create(self, direction: str, kind: str, amount, issue_date: str, due_date: str, party_id: int | None = None,
               number: str = "", bank: str = "", notes: str = "") -> int:
        if direction not in DIRECTIONS or kind not in KINDS:
            raise ValidationError("Geçersiz çek / senet türü.")
        amount = money(amount, "Tutar", positive=True)
        issue_date = validate_iso_date(issue_date, "Düzenleme tarihi", required=True)
        due_date = validate_iso_date(due_date, "Vade tarihi", required=True)
        if due_date < issue_date:
            raise ValidationError("Vade tarihi düzenleme tarihinden önce olamaz.")
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO fin_notes(direction, kind, party_id, number, bank, amount, issue_date, due_date, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (direction, kind, party_id, clean(number) or "", clean(bank) or "", to_db_text(amount, 2),
                 issue_date, due_date, clean(notes) or ""))
            audit.log(self.conn, "fin_note", cur.lastrowid, "EKLE", new={"direction": direction, "kind": kind,
                                                                         "amount": str(amount)})
        return cur.lastrowid

    def set_status(self, note_id: int, status: str, account_id: int | None = None, when: str | None = None) -> None:
        n = self.note(note_id)
        if status not in STATUSES:
            raise ValidationError("Geçersiz durum.")
        if status == "TAHSIL" and n.direction != "ALINAN" or status == "ODENDI" and n.direction != "VERILEN":
            raise ValidationError("Alınan çek/senet tahsil edilir, verilen çek/senet ödenir.")
        when = validate_iso_date(when, "Tarih") or date.today().isoformat()
        old = self.conn.execute("SELECT move_id FROM fin_notes WHERE id = ?", (note_id,)).fetchone()["move_id"]
        with transaction(self.conn):
            if old:
                self.conn.execute("UPDATE fin_notes SET move_id = NULL WHERE id = ?", (note_id,))
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (old,))
            move_id = None
            if account_id and status in ("TAHSIL", "ODENDI"):
                sign = n.amount if status == "TAHSIL" else -n.amount
                text = f"{n.label} {n.number}".strip() + (f" · {n.party_name}" if n.party_name else "")
                move_id = insert_move(self.conn, account_id, when, sign, text, "SENET")
            self.conn.execute("UPDATE fin_notes SET status = ?, move_id = ? WHERE id = ?", (status, move_id, note_id))
            audit.log(self.conn, "fin_note", note_id, "GUNCELLE", old={"status": n.status}, new={"status": status})

    def delete(self, note_id: int) -> None:
        n = self.note(note_id)
        move = self.conn.execute("SELECT move_id FROM fin_notes WHERE id = ?", (note_id,)).fetchone()["move_id"]
        with transaction(self.conn):
            self.conn.execute("DELETE FROM fin_notes WHERE id = ?", (note_id,))
            if move:
                self.conn.execute("DELETE FROM fin_moves WHERE id = ?", (move,))
            audit.log(self.conn, "fin_note", note_id, "SIL", old={"number": n.number, "amount": str(n.amount)})
