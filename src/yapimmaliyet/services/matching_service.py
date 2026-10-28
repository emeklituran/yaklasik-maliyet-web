"""Keşif kalemlerinin kütüphane pozlarıyla eşleştirilmesi.

- Otomatik eşleştirme yalnızca TAM KOD eşleşmesinde ÖNERİ üretir (ONERILDI); onaylamaz.
- Onay her zaman kullanıcı işlemidir. Birim uyuşmazlığında onay için ayrıca açık kabul gerekir.
- Açıklama benzerliği yalnızca aday listesi üretir.
- Onaylanan özel poz sürümü kilitlenir (sonradan değişip hesapları etkilemesin diye).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, estimate_items, positions, projects
from yapimmaliyet.db.repositories import custom_positions as custom_repo
from yapimmaliyet.domain import matching
from yapimmaliyet.domain.matching import CUSTOM, OFFICIAL, Candidate, CandidateSource
from yapimmaliyet.domain.models import SearchFilter
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import ConflictError, NotFoundError


# Fiyat güncellemesinde geri alma için saklanan kalem sütunları (fiyat kopyası ve birim uyuşmazlığı onayı).
_SNAP_COLS = ("position_price_id", "snap_code", "snap_description", "snap_unit", "snap_price", "snap_source",
              "snap_valid", "snap_book_id", "snap_year", "snap_authority", "snap_period", "unit_mismatch_ack")


@dataclass(frozen=True)
class PriceChange:
    item_id: int
    line_no: int
    code: str
    description: str
    old_price: Decimal
    new_price: Decimal
    old_unit: str
    new_unit: str
    old_description: str
    new_description: str
    old_source: str = ""      # kurum — dönem (keşifteki kopya)
    new_source: str = ""      # kurum — dönem (kütüphanedeki güncel kayıt)

    @property
    def changed_price(self) -> bool:
        return self.old_price != self.new_price

    @property
    def diff(self) -> Decimal:
        return self.new_price - self.old_price

    @property
    def diff_pct(self) -> Decimal | None:
        return None if not self.old_price else (self.diff / self.old_price * 100).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PriceRefreshReport:
    changes: list[PriceChange]
    unchanged: int
    detached: int          # kaynak kitap silinmiş / poz kayıtta yok: güncellenemez, keşifteki kopya geçerlidir


@dataclass(frozen=True)
class AutoMatchReport:
    suggested: int
    unit_mismatch: int
    not_found: int
    skipped: int


class MatchingService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _estimate(self, estimate_id: int):
        e = projects.get_estimate(self.conn, estimate_id)
        if e is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        if e.locked:
            raise ConflictError("Keşif kilitli; eşleştirme değiştirilemez.")
        return e

    def _item(self, item_id: int):
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        self._estimate(it.estimate_id)
        return it

    # --- aday kaynakları ---------------------------------------------------
    def _official_by_code(self, code: str, priority: list[int]) -> list[CandidateSource]:
        rows = self.conn.execute(
            "SELECT pp.id, p.code, pp.description, pp.unit, pp.unit_price, pb.id AS book_id, pb.title, pb.year,"
            " pb.edition, pb.status, COALESCE(a.short_name, a.name) AS auth FROM positions p"
            " JOIN position_prices pp ON pp.position_id = p.id JOIN price_books pb ON pb.id = pp.price_book_id"
            " JOIN authorities a ON a.id = pb.authority_id WHERE p.code_norm = ?", (normalize_code(code),))
        return [self._source(r) for r in rows if r["status"] != "TASLAK" or r["book_id"] in priority]

    @staticmethod
    def _source(r) -> CandidateSource:
        ed =f" {r['edition']}" if r["edition"] else ""
        return CandidateSource(OFFICIAL, r["id"], r["code"], r["description"], r["unit"], from_db_text(r["unit_price"]),
                               f"{r['auth']} — {r['title']} {r['year']}{ed}", r["book_id"])

    def _official_by_text(self, text: str, priority: list[int]) -> list[CandidateSource]:
        toks = sorted(matching.tokens(text), key=len, reverse=True)[:6]
        if not toks:
            return []
        q = " OR ".join(f'"{t}"*' for t in toks)
        rows = self.conn.execute(
            "SELECT pp.id, p.code, pp.description, pp.unit, pp.unit_price, pb.id AS book_id, pb.title, pb.year,"
            " pb.edition, pb.status, COALESCE(a.short_name, a.name) AS auth FROM position_prices pp"
            " JOIN positions p ON p.id = pp.position_id JOIN price_books pb ON pb.id = pp.price_book_id"
            " JOIN authorities a ON a.id = pb.authority_id"
            " WHERE pp.id IN (SELECT rowid FROM position_prices_fts WHERE position_prices_fts MATCH ? LIMIT 300)",
            (q,))
        return [self._source(r) for r in rows if r["status"] != "TASLAK" or r["book_id"] in priority]

    def _custom_sources(self) -> list[CandidateSource]:
        out = []
        for cp in custom_repo.list_positions(self.conn):
            v = cp.current
            if v is not None:
                out.append(CandidateSource(CUSTOM, v.id, cp.code, f"{cp.name} — {v.description}", v.unit,
                                           v.effective_price, f"Özel poz (sürüm {v.version_no})"))
        return out

    def candidates(self, item_id: int, limit: int = 10) -> list[Candidate]:
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        prio = projects.get_estimate(self.conn, it.estimate_id).source_priority
        sources: dict[tuple[str, int], CandidateSource] = {}
        if it.code:
            base = matching.split_suffix(it.code)
            for c in (it.code, base[0]) if base else (it.code,):
                for s in self._official_by_code(c, prio):
                    sources[(s.kind, s.ref_id)] = s
        for s in self._official_by_text(it.description or "", prio) + self._custom_sources():
            sources.setdefault((s.kind, s.ref_id), s)
        return matching.rank(it.code, it.description, it.unit, list(sources.values()), prio, limit)

    # --- otomatik öneri ----------------------------------------------------
    def auto_match(self, estimate_id: int) -> AutoMatchReport:
        e = self._estimate(estimate_id)
        suggested = mismatch = not_found = skipped = 0
        with transaction(self.conn):
            for it in estimate_items.list_items(self.conn, estimate_id):
                if it.match_status in ("ONAYLANDI", "ERTELENDI") or not it.code:
                    skipped += 1
                    continue
                exact = [c for c in matching.rank(it.code, None, it.unit,
                                                  self._official_by_code(it.code, e.source_priority),
                                                  e.source_priority) if c.exact_code]
                if not exact:
                    not_found += 1
                    if it.match_status == "ONERILDI":
                        estimate_items.set_match(self.conn, it.id, "ESLESMEDI", None, None, False, None)
                    continue
                best = exact[0]
                estimate_items.set_match(self.conn, it.id, "ONERILDI", best.source.ref_id, None, False, "OTOMATIK")
                suggested += 1
                mismatch += best.unit_ok is False
            audit.log(self.conn, "estimate", estimate_id, "TOPLU",
                      new={"suggested": suggested, "unit_mismatch": mismatch}, note="Otomatik eşleştirme önerisi")
        return AutoMatchReport(suggested, mismatch, not_found, skipped)

    # --- kullanıcı işlemleri -----------------------------------------------
    def approve(self, item_id: int, accept_unit_mismatch: bool = False) -> None:
        it = self._item(item_id)
        if it.position_price_id is None and it.custom_version_id is None:
            raise ConflictError("Onaylanacak bir eşleşme yok.")
        self._assign(it, it.position_price_id, it.custom_version_id, accept_unit_mismatch, it.matched_by or "KULLANICI")

    def approve_all_suggestions(self, estimate_id: int) -> int:
        """Birimi uyuşan tüm önerileri onaylar (kullanıcının toplu onayı). Uyuşmayanlar tek tek onaylanmalıdır."""
        self._estimate(estimate_id)
        n = 0
        with transaction(self.conn):
            for it in estimate_items.list_items(self.conn, estimate_id):
                if it.match_status == "ONERILDI" and not it.unit_mismatch:
                    estimate_items.set_match(self.conn, it.id, "ONAYLANDI", it.position_price_id, it.custom_version_id,
                                             False, it.matched_by or "OTOMATIK")
                    if it.custom_version_id:
                        custom_repo.lock_version(self.conn, it.custom_version_id)
                    n += 1
            audit.log(self.conn, "estimate", estimate_id, "TOPLU", new={"approved": n}, note="Önerilerin toplu onayı")
        return n

    def assign_official(self, item_id: int, price_id: int, accept_unit_mismatch: bool = False) -> None:
        it = self._item(item_id)
        if positions.get(self.conn, price_id) is None:
            raise NotFoundError("Poz fiyat kaydı bulunamadı.")
        self._assign(it, price_id, None, accept_unit_mismatch, "KULLANICI")

    def assign_custom(self, item_id: int, version_id: int, accept_unit_mismatch: bool = False) -> None:
        it = self._item(item_id)
        if self.conn.execute("SELECT 1 FROM custom_position_versions WHERE id = ?", (version_id,)).fetchone() is None:
            raise NotFoundError("Özel poz sürümü bulunamadı.")
        self._assign(it, None, version_id, accept_unit_mismatch, "KULLANICI")

    def _assign(self, it, price_id: int | None, version_id: int | None, accept: bool, by: str) -> None:
        with transaction(self.conn):
            estimate_items.set_match(self.conn, it.id, "ONAYLANDI", price_id, version_id, False, by)
            updated = estimate_items.get_item(self.conn, it.id)
            if updated.unit_mismatch and not accept:
                raise ConflictError(
                    f"Birim uyuşmuyor: keşifte '{updated.unit}', seçilen pozda '{updated.matched_unit}'. "
                    "Bu eşleşmeyi kullanmak için birim farkını açıkça kabul etmelisiniz.")
            if updated.unit_mismatch:
                estimate_items.set_match(self.conn, it.id, "ONAYLANDI", price_id, version_id, True, by)
            if version_id:
                custom_repo.lock_version(self.conn, version_id)
            audit.log(self.conn, "estimate_item", it.id, "GUNCELLE",
                      old={"status": it.match_status, "price_id": it.position_price_id,
                           "custom_version_id": it.custom_version_id},
                      new={"status": "ONAYLANDI", "price_id": price_id, "custom_version_id": version_id,
                           "unit_mismatch_ack": updated.unit_mismatch})

    def defer(self, item_id: int) -> None:
        it = self._item(item_id)
        with transaction(self.conn):
            estimate_items.set_match(self.conn, item_id, "ERTELENDI", None, None, False, None)
            audit.log(self.conn, "estimate_item", item_id, "GUNCELLE", old={"status": it.match_status},
                      new={"status": "ERTELENDI"})

    def unmatch(self, item_id: int) -> None:
        it = self._item(item_id)
        with transaction(self.conn):
            estimate_items.set_match(self.conn, item_id, "ESLESMEDI", None, None, False, None)
            audit.log(self.conn, "estimate_item", item_id, "GUNCELLE", old={"status": it.match_status},
                      new={"status": "ESLESMEDI"})

    # --- kütüphane fiyatı değişince keşifteki birim fiyatları güncelleme --------------------------------
    def price_changes(self, estimate_id: int) -> PriceRefreshReport:
        """Keşif kalemlerinin kendi kopyası ile kütüphanedeki güncel poz kaydının karşılaştırması (yalnız okuma)."""
        changes, same, detached = [], 0, 0
        for it in estimate_items.list_items(self.conn, estimate_id):
            if it.position_price_id is None:
                continue
            live = self.conn.execute(
                "SELECT p.code, pp.description, pp.unit, pp.unit_price, pb.hidden, pb.valid_from, pb.period, pb.year,"
                " COALESCE(a.short_name, a.name) AS auth FROM position_prices pp"
                " JOIN positions p ON p.id = pp.position_id JOIN price_books pb ON pb.id = pp.price_book_id"
                " JOIN authorities a ON a.id = pb.authority_id WHERE pp.id = ?", (it.position_price_id,)).fetchone()
            if live is None or live["hidden"]:
                detached += 1
                continue
            price = from_db_text(live["unit_price"])
            snap = self.conn.execute("SELECT snap_valid, snap_authority, snap_period, snap_year FROM estimate_items"
                                     " WHERE id = ?", (it.id,)).fetchone()
            if (price, live["unit"], live["description"], live["valid_from"]) == (
                    it.matched_price, it.matched_unit, it.matched_description, snap["snap_valid"]):
                same += 1
                continue
            old_src = f"{snap['snap_authority'] or '—'} — {snap['snap_period'] or snap['snap_year'] or 'Belirtilmemiş'}"
            new_src = f"{live['auth']} — {live['period'] or live['year']}"
            changes.append(PriceChange(it.id, it.line_no, live["code"], it.matched_description or "", it.matched_price,
                                       price, it.matched_unit or "", live["unit"], it.matched_description or "",
                                       live["description"], old_src, new_src))
        return PriceRefreshReport(changes, same, detached)

    def refresh_prices(self, estimate_id: int, item_ids: list[int] | None = None) -> int:
        """Seçilen (ya da tüm) kalemlerin kopyasını kütüphanedeki güncel poz kaydıyla yeniler; kullanıcı fiyat
        değişiklikleri ellenmez. Birim değiştiyse birim uyuşmazlığı onayı sıfırlanır. Döndürür: güncellenen kalem sayısı."""
        self._estimate(estimate_id)
        report = self.price_changes(estimate_id)
        todo = [c for c in report.changes if item_ids is None or c.item_id in item_ids]
        if not todo:
            return 0
        with transaction(self.conn):
            rev = self.conn.execute("INSERT INTO price_revisions(estimate_id, note, item_count) VALUES (?, ?, ?)",
                                    (estimate_id, "Birim fiyatlar kütüphaneden güncellendi", len(todo))).lastrowid
            cols = ", ".join(_SNAP_COLS)
            for c in todo:   # geri alma için kalemin güncelleme öncesi fiyat kopyası
                row = self.conn.execute(f"SELECT {cols} FROM estimate_items WHERE id = ?", (c.item_id,)).fetchone()
                self.conn.execute("INSERT INTO price_revision_items(revision_id, estimate_item_id, snapshot_json)"
                                  " VALUES (?, ?, ?)", (rev, c.item_id, json.dumps(dict(row), ensure_ascii=False)))
            for c in todo:
                it = estimate_items.get_item(self.conn, c.item_id)
                estimate_items.take_snapshot(self.conn, c.item_id, it.position_price_id)
                if c.old_unit != c.new_unit:
                    self.conn.execute("UPDATE estimate_items SET unit_mismatch_ack = 0 WHERE id = ?", (c.item_id,))
                audit.log(self.conn, "estimate_item", c.item_id, "GUNCELLE",
                          old={"price": c.old_price, "unit": c.old_unit}, new={"price": c.new_price, "unit": c.new_unit},
                          note="Birim fiyat kütüphaneden güncellendi")
        return len(todo)

    def revisions(self, estimate_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM price_revisions WHERE estimate_id = ? ORDER BY id DESC",
                                 (estimate_id,)).fetchall()

    def undo_revision(self, revision_id: int) -> int:
        """Fiyat güncellemesini geri alır: kalemlerin güncelleme öncesi kopyası geri yazılır. Döndürür: kalem sayısı."""
        rev = self.conn.execute("SELECT * FROM price_revisions WHERE id = ?", (revision_id,)).fetchone()
        if rev is None:
            raise NotFoundError("Fiyat revizyonu bulunamadı.")
        if rev["undone_at"]:
            raise ConflictError("Bu güncelleme zaten geri alındı.")
        self._estimate(rev["estimate_id"])
        rows = self.conn.execute("SELECT estimate_item_id, snapshot_json FROM price_revision_items WHERE revision_id = ?",
                                 (revision_id,)).fetchall()
        with transaction(self.conn):
            for r in rows:
                snap = json.loads(r["snapshot_json"])
                cols = [c for c in _SNAP_COLS if c in snap]
                self.conn.execute(f"UPDATE estimate_items SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                                  (*(snap[c] for c in cols), r["estimate_item_id"]))
            self.conn.execute("UPDATE price_revisions SET undone_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime')"
                              " WHERE id = ?", (revision_id,))
            audit.log(self.conn, "estimate", rev["estimate_id"], "GUNCELLE", new={"undo_revision": revision_id},
                      note="Birim fiyat güncellemesi geri alındı")
        return len(rows)

    def search_library(self, text: str, limit: int = 100, book_ids: list[int] | None = None,
                       authority_id: int | None = None):
        """Elle eşleştirme için kütüphane araması (taslak kitaplar dahil değil).

        `book_ids`: yalnız bu kitaplarda ara; `authority_id`: yalnız bu idarenin kitaplarında ara. Metin boşsa
        seçilen kitap(lar)ın ilk kayıtları listelenir (gezinme)."""
        f = SearchFilter(text=text, include_archived=True, authority_id=authority_id, exclude_drafts=True,
                         book_ids=tuple(book_ids) if book_ids is not None else None)
        return positions.search(self.conn, f, 0, limit)

    def library_books(self) -> list[dict]:
        """Aramada seçilebilecek kitaplar (taslak ve boş kitaplar hariç): id, ad, idare."""
        rows = self.conn.execute(
            "SELECT pb.id, pb.title, pb.year, pb.edition, pb.status, a.id AS authority_id,"
            " COALESCE(a.short_name, a.name) AS authority, (SELECT COUNT(*) FROM position_prices pp"
            "  WHERE pp.price_book_id = pb.id) AS n FROM price_books pb JOIN authorities a ON a.id = pb.authority_id"
            " WHERE pb.status <> 'TASLAK' ORDER BY a.name, pb.year DESC, pb.id DESC")
        return [{"id": r["id"], "authority_id": r["authority_id"], "authority": r["authority"], "count": r["n"],
                 "label": f"{r['title']} {r['year']}" + (f" {r['edition']}" if r["edition"] else "")}
                for r in rows if r["n"]]

    def estimate_book_ids(self, estimate_id: int) -> list[int]:
        e = projects.get_estimate(self.conn, estimate_id)
        return list(e.source_priority) if e else []
