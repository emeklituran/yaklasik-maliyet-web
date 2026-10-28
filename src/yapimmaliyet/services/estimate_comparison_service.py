"""Mukayeseli keşif servisi: kayıt, hesap (iki keşif aynı profille hesap motorundan), kararlar ve analiz bağlantısı.

Birim fiyatlar ve tutarlar her keşfin KENDİ eşleştirmesinden/fiyat kaynağından gelir (keşifler değiştirilmez).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, estimate_items, projects
from yapimmaliyet.domain.estimate_compare import Comparison, Line, compare
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services import library_compare_service as lcs
from yapimmaliyet.services.calc_service import CalcService


@dataclass(frozen=True)
class ComparisonInfo:
    id: int
    name: str
    first_estimate_id: int
    second_estimate_id: int
    profile_id: int | None
    notes: str | None
    created_at: str


class EstimateComparisonService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.calc = CalcService(conn)

    # --- kayıtlar ----------------------------------------------------------
    def create(self, name: str, first_id: int, second_id: int, profile_id: int | None, notes: str | None = None) -> int:
        name = (name or "").strip()
        if not name:
            raise ValidationError("Karşılaştırma adı boş bırakılamaz.")
        if first_id == second_id:
            raise ValidationError("İlk ve son keşif farklı olmalıdır.")
        for e in (first_id, second_id):
            if projects.get_estimate(self.conn, e) is None:
                raise NotFoundError("Keşif bulunamadı.")
        with transaction(self.conn):
            cid = self.conn.execute(
                "INSERT INTO estimate_comparisons(name, first_estimate_id, second_estimate_id, profile_id, notes)"
                " VALUES (?, ?, ?, ?, ?)", (name, first_id, second_id, profile_id, notes)).lastrowid
            audit.log(self.conn, "estimate_comparison", cid, "EKLE",
                      new={"first": first_id, "second": second_id, "profile": profile_id})
        return cid

    def list(self, project_id: int | None = None) -> list[ComparisonInfo]:
        """Karşılaştırmalar; proje verilirse keşiflerinden biri o projede olanlar."""
        sql = ("SELECT id, name, first_estimate_id, second_estimate_id, profile_id, notes, created_at"
               " FROM estimate_comparisons")
        params: tuple = ()
        if project_id is not None:
            sql += (" WHERE EXISTS (SELECT 1 FROM estimates e WHERE e.project_id = ? AND e.id IN"
                    " (first_estimate_id, second_estimate_id))")
            params = (project_id,)
        return [ComparisonInfo(*r) for r in self.conn.execute(sql + " ORDER BY id DESC", params)]

    def get(self, cid: int) -> ComparisonInfo:
        r = self.conn.execute("SELECT id, name, first_estimate_id, second_estimate_id, profile_id, notes, created_at"
                              " FROM estimate_comparisons WHERE id = ?", (cid,)).fetchone()
        if r is None:
            raise NotFoundError("Karşılaştırma bulunamadı.")
        return ComparisonInfo(*r)

    def delete(self, cid: int) -> None:
        self.get(cid)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM estimate_comparisons WHERE id = ?", (cid,))
            audit.log(self.conn, "estimate_comparison", cid, "SIL")

    def decide(self, cid: int, first_item_id: int, second_item_id: int, approve: bool, note: str | None = None) -> None:
        self.get(cid)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO comparison_decisions(comparison_id, first_item_id, second_item_id, decision, note)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(comparison_id, first_item_id, second_item_id) DO UPDATE SET"
                " decision = excluded.decision, note = excluded.note,"
                " decided_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')",
                (cid, first_item_id, second_item_id, "ONAY" if approve else "RET", note))
            audit.log(self.conn, "estimate_comparison", cid, "GUNCELLE",
                      new={"first_item": first_item_id, "second_item": second_item_id, "approve": approve})

    def clear_decision(self, cid: int, first_item_id: int, second_item_id: int) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM comparison_decisions WHERE comparison_id = ? AND first_item_id = ?"
                              " AND second_item_id = ?", (cid, first_item_id, second_item_id))

    # --- hesap ---------------------------------------------------------------
    def _lines(self, estimate_id: int, profile_id: int) -> list[Line]:
        result = self.calc.calculate(estimate_id, profile_id)
        return [Line(ln.input.item_id, ln.input.line_no, ln.input.code, ln.input.description, ln.input.unit,
                     ln.input.quantity, ln.unit_price, ln.amount,
                     ln.input.price_source + (f" ({ln.input.price_date})" if ln.input.price_date else ""))
                for ln in result.lines]

    def compute(self, cid: int) -> Comparison:
        info = self.get(cid)
        profile = info.profile_id or self.default_profile()
        decisions = self.conn.execute("SELECT first_item_id, second_item_id, decision FROM comparison_decisions"
                                      " WHERE comparison_id = ?", (cid,)).fetchall()
        approved = {(a, b) for a, b, d in decisions if d == "ONAY"}
        rejected = {(a, b) for a, b, d in decisions if d == "RET"}
        cmp = compare(self._lines(info.first_estimate_id, profile), self._lines(info.second_estimate_id, profile),
                      approved, rejected)
        prof = self.calc.get_profile(profile)
        cmp.notes.insert(0, f"Hesap profili: {prof.name} (sürüm {prof.version}). Fiyatlar her keşfin kendi "
                            "eşleştirmesinden ve fiyat kaynağından alındı.")
        return cmp

    def default_profile(self) -> int:
        profs = [p for p in self.calc.profiles() if p.kind == "YAKLASIK"] or self.calc.profiles()
        if not profs:
            raise ValidationError("Hesap profili bulunamadı.")
        return profs[0].id

    # --- analiz bağlantısı -----------------------------------------------------
    def analysis_side(self, item_id: int):
        """Kalemin fiyatının dayandığı analiz: özel poz analizi ya da aynı kodlu resmî analiz (yoksa None)."""
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        if it.custom_version_id:
            try:
                return lcs.custom_side(self.conn, it.custom_version_id)
            except ValidationError:
                return None
        code = it.matched_code or it.code
        if not code:
            return None
        r = self.conn.execute("SELECT oa.id FROM official_analyses oa JOIN analysis_books ab ON ab.id ="
                              " oa.analysis_book_id WHERE oa.code_norm = ? ORDER BY ab.year DESC, oa.id DESC LIMIT 1",
                              (normalize_code(code),)).fetchone()
        return lcs.official_side(self.conn, r[0]) if r else None
