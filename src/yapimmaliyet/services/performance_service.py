"""Alt yüklenici performans göstergeleri (şartname N2).

İLKE: eksik veriden puan üretilmez. Her gösterge kendi kaynağından, formülü açıkça yazılı hesaplanır; veri yoksa değer yerine
"Veri yok" ve nedeni gösterilir (0 varsayılmaz). Birleşik/ağırlıklı bir "genel puan" HESAPLANMAZ; kapsam yalnız "kaç gösterge
ölçülebildi" olarak bildirilir. Kullanıcı notu (1–5) elle girilen değerlendirmedir, göstergelerden türetilmez.

Göstergeler (taşeron sözleşmesi başına):
  TEKLIF_FARKI   Seçilen teklif toplamı, aynı paketteki diğer geçerli ve kapsamı tam tekliflerin ortalamasından % kaç farklı
  SOZLESME_ARTISI Onaylı iş artışı/eksilişi, asıl sözleşme bedelinin %'si (onay bekleyenler hariç)
  SURE_UZATIMI   Onaylı süre uzatımı gün, asıl sözleşme süresinin %'si (başlangıç/bitiş tarihi gerekir)
  FAALIYET_GECIKMESI Bağlı iş paketinin vadesi gelen faaliyetlerinden %100'e ulaşmamış olanların oranı
  HAKEDIS_DUZELTME Düzeltilmiş (revizyonlu) hakediş sayısının hakediş sayısına oranı — resmî uyuşmazlık kaydı değildir
  TAMAMLANAN_IS  Geçerli hakedişlerle gerçekleşen tutarın güncel sözleşme bedeline oranı
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.activity_plan_service import ActivityPlanService
from yapimmaliyet.services.commitment_service import CommitmentService
from yapimmaliyet.services.contract_service import ContractService
from yapimmaliyet.services.subcontract_service import SubcontractService
from yapimmaliyet.services.validators import require

ZERO = Decimal(0)
HUNDRED = Decimal(100)
INDICATORS = {
    "TEKLIF_FARKI": ("Teklif farkı", "%", "(seçilen teklif − diğer geçerli tekliflerin ortalaması) ÷ ortalama; en az bir kıyas teklifi gerekir"),
    "SOZLESME_ARTISI": ("Sözleşme bedeli değişimi", "%", "onaylı (artış − eksiliş) ÷ asıl sözleşme bedeli"),
    "SURE_UZATIMI": ("Süre uzatımı", "%", "onaylı uzatım günü ÷ asıl sözleşme süresi (başlangıç ve bitiş tarihi gerekir)"),
    "FAALIYET_GECIKMESI": ("Geciken faaliyet oranı", "%", "vadesi gelen bağlı faaliyetlerden %100 olmayanların oranı"),
    "HAKEDIS_DUZELTME": ("Hakediş düzeltme oranı", "%", "revizyonlu hakediş sayısı ÷ hakediş sayısı (uyuşmazlık kaydı değildir)"),
    "TAMAMLANAN_IS": ("Tamamlanan iş", "%", "geçerli hakediş tutarı ÷ güncel sözleşme bedeli"),
}


@dataclass(frozen=True)
class Indicator:
    key: str
    label: str
    value: Decimal | None            # None → ölçülemedi
    unit: str
    basis: str                       # formül / dayanak
    detail: str                      # hesapta kullanılan sayılar ya da veri yok nedeni

    @property
    def measured(self) -> bool:
        return self.value is not None


@dataclass
class ContractPerformance:
    contract_id: int
    project_id: int | None
    project: str
    title: str
    indicators: list[Indicator]

    @property
    def measured(self) -> int:
        return sum(1 for i in self.indicators if i.measured)


@dataclass
class FirmPerformance:
    party_id: int
    as_of: str
    contracts: list[ContractPerformance] = field(default_factory=list)
    notes: list[sqlite3.Row] = field(default_factory=list)

    @property
    def coverage(self) -> str:
        total = sum(len(c.indicators) for c in self.contracts)
        got = sum(c.measured for c in self.contracts)
        if not self.contracts:
            return "Taşeron sözleşmesi yok: gösterge hesaplanamadı."
        return f"{got}/{total} gösterge ölçülebildi" + ("; genel puan üretilmez." if got < total else "; genel puan yine de üretilmez (göstergeler birbirine eklenmez).")

    @property
    def average_rating(self) -> Decimal | None:
        return quantize(Decimal(sum(n["rating"] for n in self.notes)) / len(self.notes), 2) if self.notes else None


def _pct(num: Decimal, den: Decimal) -> Decimal:
    return quantize(num * HUNDRED / den, 2)


class PerformanceService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractService(conn)
        self.sub = SubcontractService(conn)

    # --- kullanıcı notları ----------------------------------------------------
    def add_note(self, party_id: int, rating: int, note: str, rated_by: str, contract_id: int | None = None) -> int:
        if self.conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
            raise NotFoundError("Firma bulunamadı.")
        if not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValidationError("Puan 1 ile 5 arasında olmalıdır.")
        if contract_id is not None:
            c = self.conn.execute("SELECT contractor_party_id FROM pp_contracts WHERE id = ?", (contract_id,)).fetchone()
            if c is None or c["contractor_party_id"] != party_id:
                raise ValidationError("Seçilen sözleşme bu firmaya ait değil.")
        with transaction(self.conn):
            nid = self.conn.execute("INSERT INTO sub_perf_notes(party_id, contract_id, rating, note, rated_by) VALUES (?, ?, ?, ?, ?)",
                                    (party_id, contract_id, rating, require(note, "Gerekçe / not"), require(rated_by, "Değerlendiren"))).lastrowid
            audit.log(self.conn, "sub_perf_note", nid, "EKLE", new={"party": party_id, "rating": rating})
        return nid

    def notes(self, party_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sub_perf_notes WHERE party_id = ? ORDER BY id DESC", (party_id,)).fetchall()

    # --- göstergeler --------------------------------------------------------------
    def evaluate(self, party_id: int, as_of: str | None = None) -> FirmPerformance:
        if self.conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
            raise NotFoundError("Firma bulunamadı.")
        as_of = as_of or date.today().isoformat()
        out = FirmPerformance(party_id, as_of, notes=self.notes(party_id))
        for c in self.conn.execute(
                "SELECT c.id, c.project_id, c.title, c.sub_bid_id, c.start_date, c.end_date, p.name AS project FROM pp_contracts c"
                " LEFT JOIN projects p ON p.id = c.project_id WHERE c.kind = 'TASERON' AND c.contractor_party_id = ? ORDER BY c.id", (party_id,)):
            out.contracts.append(ContractPerformance(c["id"], c["project_id"], c["project"] or "—", c["title"], self._indicators(c, as_of)))
        return out

    def _indicators(self, c: sqlite3.Row, as_of: str) -> list[Indicator]:
        def ind(key, value, detail):
            label, unit, basis = INDICATORS[key]
            return Indicator(key, label, value, unit, basis, detail)

        summary = self.contracts.summary(c["id"])
        res = []
        # teklif farkı
        pkg = None
        if c["sub_bid_id"]:
            pkg = self.conn.execute("SELECT package_id FROM sub_bids WHERE id = ?", (c["sub_bid_id"],)).fetchone()
        if pkg is None:
            res.append(ind("TEKLIF_FARKI", None, "Sözleşme bir alt yüklenici teklifine bağlı değil."))
        else:
            bids = self.sub.compare(pkg["package_id"], as_of).bids
            mine = next((b for b in bids if b.bid_id == c["sub_bid_id"]), None)
            others = [b for b in bids if b.bid_id != c["sub_bid_id"] and b.complete and b.total > 0]
            if mine is None or not mine.complete:
                res.append(ind("TEKLIF_FARKI", None, "Seçilen teklif geçerli ya da kapsamı tam değil."))
            elif not others:
                res.append(ind("TEKLIF_FARKI", None, "Kıyaslanabilecek başka geçerli ve kapsamı tam teklif yok."))
            else:
                avg = sum((b.total for b in others), ZERO) / len(others)
                res.append(ind("TEKLIF_FARKI", _pct(mine.total - avg, avg), f"seçilen {mine.total}, {len(others)} teklifin ortalaması {quantize(avg, 2)}"))
        # sözleşme artışı
        base = summary.contract.contract_amount
        if base > 0:
            res.append(ind("SOZLESME_ARTISI", _pct(summary.amount_delta, base), f"asıl {base}, onaylı değişim {summary.amount_delta}"
                           + (f"; onay bekleyen {len(summary.pending)} kayıt hariç" if summary.pending else "")))
        else:
            res.append(ind("SOZLESME_ARTISI", None, "Asıl sözleşme bedeli 0."))
        # süre uzatımı
        try:
            dur = (date.fromisoformat(c["end_date"]) - date.fromisoformat(c["start_date"])).days if c["start_date"] and c["end_date"] else 0
        except ValueError:
            dur = 0
        if dur > 0:
            res.append(ind("SURE_UZATIMI", _pct(Decimal(summary.days), Decimal(dur)), f"{summary.days} gün / asıl süre {dur} gün"))
        else:
            res.append(ind("SURE_UZATIMI", None, "Sözleşmede başlangıç ve bitiş tarihi (geçerli süre) yok."))
        # faaliyet gecikmesi
        acts = ActivityPlanService(self.conn).activities(c["project_id"]) if (c["project_id"] and pkg) else []
        due = [a for a in acts if a.package_id == pkg["package_id"] and a.planned_end <= as_of] if pkg else []
        if not pkg or not any(a.package_id == pkg["package_id"] for a in acts):
            res.append(ind("FAALIYET_GECIKMESI", None, "Paket bir faaliyete bağlı değil."))
        elif not due:
            res.append(ind("FAALIYET_GECIKMESI", None, "Bağlı faaliyetlerin hiçbirinin vadesi henüz gelmedi."))
        else:
            late = [a for a in due if a.progress < 100]
            res.append(ind("FAALIYET_GECIKMESI", _pct(Decimal(len(late)), Decimal(len(due))), f"vadesi gelen {len(due)} faaliyetten {len(late)} tamamlanmamış"))
        # hakediş düzeltme
        pays = self.conn.execute("SELECT no, MAX(revision) AS rev FROM pp_payments WHERE contract_id = ? GROUP BY no", (c["id"],)).fetchall()
        if not pays:
            res.append(ind("HAKEDIS_DUZELTME", None, "Henüz hakediş yok."))
        else:
            fixed = sum(1 for p in pays if p["rev"] > 0)
            res.append(ind("HAKEDIS_DUZELTME", _pct(Decimal(fixed), Decimal(len(pays))), f"{len(pays)} hakedişten {fixed} tanesi düzeltilmiş"))
        # tamamlanan iş
        row = next((r for r in CommitmentService(self.conn).summary(c["project_id"], as_of).rows
                    if r.kind == "TASERON" and r.source_id == c["id"]), None) if c["project_id"] else None
        if row is None or row.realized == 0:
            res.append(ind("TAMAMLANAN_IS", None, "Geçerli (onaylı/ödenmiş) hakediş yok."))
        elif row.committed <= 0:
            res.append(ind("TAMAMLANAN_IS", None, "Güncel sözleşme bedeli 0."))
        else:
            res.append(ind("TAMAMLANAN_IS", _pct(row.realized, row.committed), f"gerçekleşen {row.realized} / sözleşme {row.committed}"))
        return res
