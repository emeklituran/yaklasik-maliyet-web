"""Örnek çalışma: dört disiplinli, açıkça "ÖRNEK / test verisi" olarak işaretli bir senaryo üretir.

Fiyatlar ve pozlar SENTETİKTİR; hiçbir resmî listeden alınmamıştır. Poz kodları "ÖRN-" önekiyle resmî
kodlardan ayrılır, kitaplar ve idare adı "ÖRNEK" içerir, teklifler `is_sample` ile işaretlenir.
Mevcut örnek idare ve kitaplar varsa yeniden kullanılır; kullanıcının diğer kayıtlarına dokunulmaz.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from yapimmaliyet.domain.models import BookStatus, NewPositionPrice
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.services.library_service import LibraryService
from yapimmaliyet.services.matching_service import MatchingService
from yapimmaliyet.services.project_service import ProjectService
from yapimmaliyet.services.tender_service import TenderService

AUTHORITY = "ÖRNEK İdare (test verisi)"
MARK = "ÖRNEK"
SAMPLE_NOTE = ("Bu çalışma programı denemek için üretilmiş SENTETİK TEST VERİSİDİR. Pozlar, birim fiyatlar ve "
               "teklifler gerçek değildir; resmî bir listeden alınmamıştır. Gerçek işlerde kullanmayın.")

D = Decimal
# disiplin → (kod, tanım, birim, sentetik birim fiyat, örnek miktar)
POSITIONS: dict[str, list[tuple[str, str, str, Decimal, Decimal]]] = {
    "INSAAT": [("ÖRN-İNŞ-01", "Makine ile yumuşak toprak kazısı (ÖRNEK)", "m³", D("120.00"), D("850")),
               ("ÖRN-İNŞ-02", "C25/30 hazır beton dökülmesi (ÖRNEK)", "m³", D("3200.00"), D("420.5")),
               ("ÖRN-İNŞ-03", "Nervürlü beton çelik çubuğu (ÖRNEK)", "ton", D("32000.00"), D("38.75")),
               ("ÖRN-İNŞ-04", "Plywood ile düz yüzeyli beton kalıbı (ÖRNEK)", "m²", D("650.00"), D("1240"))],
    "ELEKTRIK": [("ÖRN-ELK-01", "NYM 3x2,5 mm² kablo çekilmesi (ÖRNEK)", "m", D("85.00"), D("3600")),
                 ("ÖRN-ELK-02", "LED armatür 36 W (ÖRNEK)", "Ad", D("1450.00"), D("210")),
                 ("ÖRN-ELK-03", "Sıva altı anahtar (ÖRNEK)", "Ad", D("180.00"), D("160")),
                 ("ÖRN-ELK-04", "Dağıtım panosu (ÖRNEK)", "Ad", D("18500.00"), D("6"))],
    "MEKANIK": [("ÖRN-MEK-01", "PPR-C temiz su borusu Ø25 (ÖRNEK)", "m", D("140.00"), D("980")),
                ("ÖRN-MEK-02", "Panel radyatör (ÖRNEK)", "m", D("2600.00"), D("145.5")),
                ("ÖRN-MEK-03", "Lavabo (ÖRNEK)", "Ad", D("2200.00"), D("48")),
                ("ÖRN-MEK-04", "Küresel vana DN25 (ÖRNEK)", "Ad", D("450.00"), D("96"))],
    "ALTYAPI": [("ÖRN-ALT-01", "PVC atık su borusu Ø200 döşenmesi (ÖRNEK)", "m", D("520.00"), D("640")),
                ("ÖRN-ALT-02", "Beton parke taşı döşenmesi (ÖRNEK)", "m²", D("380.00"), D("2150")),
                ("ÖRN-ALT-03", "Plent-miks temel tabakası (ÖRNEK)", "m³", D("900.00"), D("310")),
                ("ÖRN-ALT-04", "Beton bordür döşenmesi (ÖRNEK)", "m", D("260.00"), D("780"))],
}
# Karma model örneği: altyapı kalemleri götürü bedel, diğerleri birim fiyat.
METHOD = {"INSAAT": "BIRIM_FIYAT", "ELEKTRIK": "BIRIM_FIYAT", "MEKANIK": "BIRIM_FIYAT", "ALTYAPI": "GOTURU"}
# Örnek teklif katsayıları (kalem sırasına göre dönerek uygulanır) — sentetik.
BID_FACTORS = {"ÖRNEK İstekli A": (D("0.91"), D("0.95"), D("0.88"), D("0.97")),
               "ÖRNEK İstekli B": (D("1.02"), D("0.99"), D("1.06"), D("1.01"))}


@dataclass(frozen=True)
class SampleResult:
    project_id: int
    estimate_id: int
    book_ids: dict[str, int]
    bid_ids: list[int]


class SampleService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.library = LibraryService(conn)
        self.projects = ProjectService(conn)

    def _authority(self) -> int:
        for a in self.library.list_authorities():
            if a.name == AUTHORITY:
                return a.id
        return self.library.add_authority(AUTHORITY, MARK, SAMPLE_NOTE)

    def _books(self, authority_id: int) -> dict[str, int]:
        year = date.today().year
        existing = {(b.edition, b.discipline): b for b in self.library.list_books(authority_id)}
        out = {}
        for disc, rows in POSITIONS.items():
            edition = f"{MARK} {DISCIPLINES[disc]}"
            book = existing.get((edition, disc))
            if book is not None and book.price_count:
                out[disc] = book.id
                continue
            bid = book.id if book else self.library.add_book(
                authority_id, f"{MARK} {DISCIPLINES[disc]} Birim Fiyatları — gerçek fiyat değildir", year, edition,
                notes=SAMPLE_NOTE)
            self.library.set_book_discipline(bid, disc)
            self.library.add_position_prices(bid, [NewPositionPrice(c, t, u, p) for c, t, u, p, _ in rows])
            self.library.set_book_status(bid, BookStatus.AKTIF)
            out[disc] = bid
        return out

    def create(self) -> SampleResult:
        books = self._books(self._authority())
        n = sum(1 for p in self.projects.list_projects() if p.name.startswith(f"{MARK} —"))
        name = f"{MARK} — Dört disiplinli okul binası (test verisi)" + (f" {n + 1}" if n else "")
        pid = self.projects.create_project(name, notes=SAMPLE_NOTE, tender_model="KARMA",
                                           disciplines=list(DISCIPLINES), work_type="Bina (örnek)")
        eid = self.projects.create_estimate(pid, "Örnek keşif (test verisi)")
        for disc, rows in POSITIONS.items():
            for code, text, unit, _, qty in rows:
                self.projects.add_item(eid, code, text, unit, qty, DISCIPLINES[disc], "Sentetik örnek kalem",
                                       discipline=disc, pricing_method=METHOD[disc])
        self.projects.set_source_priority(eid, list(books.values()))
        matching = MatchingService(self.conn)
        matching.auto_match(eid)
        matching.approve_all_suggestions(eid)
        return SampleResult(pid, eid, books, self._bids(eid))

    def _bids(self, estimate_id: int) -> list[int]:
        tender = TenderService(self.conn)
        view = tender.view(estimate_id)
        ids = []
        for bidder, factors in BID_FACTORS.items():
            lines = {}
            for i, ln in enumerate(view.lines):
                f = factors[i % len(factors)]
                if ln.ym_unit_price is None:
                    continue
                if ln.method == "GOTURU":
                    lines[ln.item_id] = quantize(ln.ym_amount * f, 2)
                else:
                    lines[ln.item_id] = quantize(ln.ym_unit_price * f, 2)
            ids.append(tender.create_bid(estimate_id, bidder, date.today().isoformat(), lines=lines,
                                         is_sample=True, notes=SAMPLE_NOTE))
        return ids
