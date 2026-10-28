"""Ekli kodlu keşif kalemleri ('/A', '/B' …) için kaynak dayanaklı fiyat ÖNERİSİ.

ÇŞB kitaplarında ekli kodun kendisi tanımlı değildir. Keşif tarifinde "montaj malzemesi bedeli dahil" yazan ve
kitapta oran pozu (Ölçü: %) bulunan durumlarda öneri: ana poz birim fiyatı × (1 + oran / 100).

- Öneri OTOMATİK UYGULANMAZ; oranın hangi tutara uygulanacağı oran pozunun tarifindedir ve kullanıcı karar verir.
- Uygulanınca kalem ana poza eşlenir, hesaplanan fiyat KULLANICI FİYATI olarak işaretlenir (resmî fiyat alanına
  yazılmaz) ve gerekçe (ana poz, oran pozu, kitap, sayfa) kalemin notuna eklenir. Tek transaction.
- '-D' (demontaj) için kaynakta kural bulunamadığından öneri üretilmez.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, estimate_items, projects, rate_positions
from yapimmaliyet.db.repositories.rate_positions import RateRow
from yapimmaliyet.domain.matching import similarity, split_suffix
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import ConflictError, NotFoundError

RATE_SUFFIXES = ("/A", "/B")


@dataclass(frozen=True)
class SuffixProposal:
    base_code: str
    base_price_id: int
    base_price: Decimal
    base_source: str
    rate: RateRow
    proposed_price: Decimal
    alternatives: tuple[RateRow, ...]

    @property
    def explanation(self) -> str:
        return (f"Öneri: {self.base_code} ({self.base_source}) {self.base_price} TL × (1 + %{self.rate.rate} "
                f"[{self.rate.code} {self.rate.description[:70]}; {self.rate.book_label}"
                f"{f', s.{self.rate.source_page}' if self.rate.source_page else ''}]) = {self.proposed_price} TL. "
                "Oranın uygulanacağı tutarı oran pozunun tarifinden kontrol edin; onayınız olmadan uygulanmaz.")


def _base_price(conn: sqlite3.Connection, code: str, priority: list[int]) -> tuple[int, Decimal, str] | None:
    order = {b: i for i, b in enumerate(priority)}
    rows = conn.execute(
        "SELECT pp.id, pp.unit_price, pb.id AS book_id, pb.title, pb.year, pb.edition FROM positions p"
        " JOIN position_prices pp ON pp.position_id = p.id JOIN price_books pb ON pb.id = pp.price_book_id"
        " WHERE p.code_norm = ? AND pb.status <> 'TASLAK'", (normalize_code(code),)).fetchall()
    rows = [r for r in rows if not priority or r["book_id"] in order]
    if not rows:
        return None
    best = min(rows, key=lambda r: order.get(r["book_id"], 999))
    return best["id"], from_db_text(best["unit_price"]), f"{best['title']} {best['year']} {best['edition']}".strip()


def propose(conn: sqlite3.Connection, item, priority: list[int]) -> SuffixProposal | None:
    """Kalem için öneri; uygun ana poz ya da aynı bölümde oran pozu yoksa None."""
    parts = split_suffix(item.code)
    if not parts or parts[1] not in RATE_SUFFIXES or "montaj malzeme" not in (item.description or "").lower():
        return None
    base_code = parts[0]
    base = _base_price(conn, base_code, priority)
    books = priority or [r[0] for r in conn.execute("SELECT id FROM price_books WHERE status <> 'TASLAK'")]
    family = base_code[:6]                                   # ör. "25.300"
    rates = [r for r in rate_positions.for_books(conn, books)
             if r.code.startswith(family) and "montaj malzeme" in r.description.lower()]
    if base is None or not rates:
        return None
    rates.sort(key=lambda r: similarity(item.description, r.description), reverse=True)
    best = rates[0]
    price = quantize(base[1] * (1 + best.rate / 100), 2)
    return SuffixProposal(base_code, base[0], base[1], base[2], best, price, tuple(rates[1:4]))


def apply(conn: sqlite3.Connection, item_id: int, proposal: SuffixProposal) -> None:
    """Kullanıcı onayından sonra: ana poza eşle + kullanıcı fiyatı + gerekçe notu (tek transaction)."""
    it = estimate_items.get_item(conn, item_id)
    if it is None:
        raise NotFoundError("Keşif kalemi bulunamadı.")
    est = projects.get_estimate(conn, it.estimate_id)
    if est.locked:
        raise ConflictError("Keşif kilitli; değişiklik yapılamaz.")
    note = (f"Ekli kod fiyatı (kullanıcı onaylı): {proposal.base_code} {proposal.base_price} TL × (1 + "
            f"%{proposal.rate.rate}, {proposal.rate.code}, {proposal.rate.book_label}"
            f"{f' s.{proposal.rate.source_page}' if proposal.rate.source_page else ''}) = {proposal.proposed_price} TL")
    notes = f"{it.notes} | {note}" if it.notes else note
    with transaction(conn):
        estimate_items.set_match(conn, it.id, "ONAYLANDI", proposal.base_price_id, None, False, "KULLANICI")
        estimate_items.set_override(conn, it.id, proposal.proposed_price)
        estimate_items.update_item(conn, it.id, it.work_group, it.code, it.description, it.unit, it.quantity, notes)
        audit.log(conn, "estimate_item", it.id, "GUNCELLE",
                  old={"status": it.match_status, "price_id": it.position_price_id,
                       "user_price_override": it.user_price_override},
                  new={"status": "ONAYLANDI", "price_id": proposal.base_price_id,
                       "user_price_override": proposal.proposed_price, "rate_position": proposal.rate.code},
                  note="Ekli kod: oran pozuna dayalı kullanıcı fiyatı")
