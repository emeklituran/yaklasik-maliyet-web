"""Firma 360°: bir firmanın (tekil firma kimliği) bütün proje, teklif, sözleşme, hakediş, satın alma ve cari
hareketlerini tek yerden okur. Hiçbir değer üretilmez; yalnız kayıtlı veriler listelenir, boş sekme boş kalır.

Her satırın isteğe bağlı bir `target` alanı vardır: (proje kimliği, sayfa adı) — ilgili kayda geçiş için.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.money import format_tr, from_db_text
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.services.cari_service import SHORT, CariService
from yapimmaliyet.services.pipeline_service import TENDER_STATUSES, WORK_STAGES

DISC = {"INSAAT": "İnşaat", "ELEKTRIK": "Elektrik", "MEKANIK": "Mekanik", "ALTYAPI": "Altyapı"}
BID_STATUS = {"GECERLI": "Geçerli", "REVIZE_EDILDI": "Revize edildi", "GERI_CEKILDI": "Geri çekildi"}
ZERO = Decimal(0)


@dataclass
class Tab:
    title: str
    columns: list[str]
    rows: list[tuple[list[str], tuple[int | None, str] | None]] = field(default_factory=list)


def _m(v) -> str:
    return format_tr(from_db_text(v)) if v not in (None, "") else ""


class FirmService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def tabs(self, party_id: int) -> list[Tab]:
        p = self._all("SELECT * FROM parties WHERE id = ?", party_id)
        if not p:
            raise ValueError("Firma bulunamadı.")
        p = p[0]
        roles = {"ISVEREN": "İşveren / İdare", "YUKLENICI": "Yüklenici", "TASERON": "Taşeron", "ISTEKLI": "İstekli",
                 "TEDARIKCI": "Tedarikçi"}
        have = [roles[r["role"]] for r in self._all("SELECT role FROM party_roles WHERE party_id = ?", party_id)]
        aliases = [r["name_key"] for r in self._all("SELECT name_key FROM party_aliases WHERE party_id = ?", party_id)]
        out = []

        t = Tab("Firma künyesi", ["Alan", "Değer"])
        for label, val in (("Ticari unvan", p["name"]), ("Kısa ad", p["short_name"]), ("Roller", ", ".join(have)),
                           ("Vergi dairesi", p["tax_office"]), ("Vergi / T.C. no", p["tax_no"]),
                           ("Adres", p["address"]), ("Durum", "Etkin" if p["active"] else "Pasif"),
                           ("Eski yazımlar (eşleşme anahtarı)", ", ".join(aliases)), ("Notlar", p["notes"])):
            if val:
                t.rows.append(([label, val], None))
        out.append(t)

        t = Tab("İletişim ve cari", ["Alan", "Değer"])
        for label, val in (("Yetkili", p["contact"]), ("Telefon", p["phone"]), ("E-posta", p["email"])):
            if val:
                t.rows.append(([label, val], None))
        cari = [s for s in CariService(self.conn).summaries(include_empty=False) if s.party_id == party_id]
        if cari:
            s = cari[0]
            t.rows += [(["Cari borç toplamı", _m(str(s.debit))], None), (["Cari alacak toplamı", _m(str(s.credit))], None),
                       (["Cari bakiye", f"{format_tr(abs(s.balance))} ({s.state})"], None),
                       (["Vadesi geçen", format_tr(s.overdue)], None)]
        for r in self._all("SELECT * FROM cari_entries WHERE party_id = ? ORDER BY entry_date DESC, id DESC LIMIT 20",
                           party_id):
            t.rows.append(([f"{format_date_tr(r['entry_date'])} · {SHORT[r['kind']]}",
                            f"{r['description']} — {_m(r['amount'])} ₺"], (r["project_id"], "Cari Hesaplar")))
        out.append(t)

        t = Tab("Verilen teklifler", ["Tür", "Proje", "Konu", "Tarih", "Tutar (KDV hariç)", "Durum"])
        for r in self._all(
                "SELECT b.id, e.project_id, pr.name AS project, e.name AS est, b.bid_date, b.lump_total FROM tender_bids b"
                " JOIN estimates e ON e.id = b.estimate_id JOIN projects pr ON pr.id = e.project_id WHERE b.party_id = ?"
                " ORDER BY b.id DESC", party_id):
            t.rows.append((["İhale teklifi", r["project"], r["est"], format_date_tr(r["bid_date"]), _m(r["lump_total"]),
                            ""], (r["project_id"], "Firma Maliyeti ve Teklif")))
        for r in self._all(
                "SELECT o.id, rq.project_id, pr.name AS project, rq.title, o.offer_date, o.status, rq.selected_offer_id"
                " FROM purchase_offers o JOIN purchase_requests rq ON rq.id = o.request_id"
                " LEFT JOIN projects pr ON pr.id = rq.project_id WHERE o.party_id = ? ORDER BY o.id DESC", party_id):
            t.rows.append((["Satın alma teklifi", r["project"] or "", r["title"], format_date_tr(r["offer_date"]), "",
                            "Seçildi" if r["selected_offer_id"] == r["id"] else r["status"]],
                           (r["project_id"], "Satın Alma Mukayesesi")))
        for r in self._all(
                "SELECT b.id, k.project_id, pr.name AS project, k.name, b.bid_date, b.status, b.revision, k.selected_bid_id"
                " FROM sub_bids b JOIN sub_packages k ON k.id = b.package_id JOIN projects pr ON pr.id = k.project_id"
                " WHERE b.party_id = ? ORDER BY b.id DESC", party_id):
            amt = sum((from_db_text(x["unit_price"]) * from_db_text(x["q"]) for x in self._all(
                "SELECT l.unit_price, i.quantity AS q FROM sub_bid_lines l JOIN sub_package_items i ON i.id = l.item_id"
                " WHERE l.bid_id = ? AND l.in_scope = 1", r["id"])), ZERO)
            vat_inc = self._all("SELECT vat_included, vat_rate FROM sub_bids WHERE id = ?", r["id"])[0]
            if vat_inc["vat_included"]:
                amt = amt / (1 + from_db_text(vat_inc["vat_rate"]) / 100)
            t.rows.append((["Alt yüklenici teklifi", r["project"], f"{r['name']} (rev. {r['revision']})",
                            format_date_tr(r["bid_date"]), format_tr(amt),
                            "Seçildi" if r["selected_bid_id"] == r["id"] else BID_STATUS[r["status"]]],
                           (r["project_id"], "Alt Yüklenici Teklifleri")))
        out.append(t)

        contracts = self._all(
            "SELECT c.*, pr.name AS project, pr.work_stage, pr.tender_status FROM pp_contracts c"
            " LEFT JOIN projects pr ON pr.id = c.project_id WHERE c.contractor_party_id = ? OR c.employer_party_id = ?"
            " ORDER BY c.id DESC", party_id, party_id)

        def crow(c) -> tuple[list[str], tuple[int | None, str]]:
            side = "Yüklenici/taşeron" if c["contractor_party_id"] == party_id else "İşveren"
            page = "Taşeron Hakedişi" if c["kind"] == "TASERON" else "Sözleşmeler ve Değişiklikler"
            stage = WORK_STAGES.get(c["work_stage"] or "", "") or TENDER_STATUSES.get(c["tender_status"] or "", "")
            return ([c["project"] or "—", c["title"], side, c["contract_no"] or "", format_date_tr(c["contract_date"]),
                     _m(c["contract_amount"]), stage], (c["project_id"], page))

        cols = ["Proje", "Sözleşme", "Taraf", "No", "Tarih", "Bedel", "Proje aşaması"]
        t = Tab("Alınan işler", cols)
        t.rows = [crow(c) for c in contracts if c["contractor_party_id"] == party_id]
        out.append(t)
        done = ("TAMAMLANDI", "KAPANDI")
        t = Tab("Devam eden sözleşmeler", cols)
        t.rows = [crow(c) for c in contracts if c["work_stage"] not in done]
        out.append(t)
        t = Tab("Tamamlanan işler", cols)
        t.rows = [crow(c) for c in contracts if c["work_stage"] in done]
        out.append(t)

        t = Tab("Disiplinler ve iş kalemleri", ["Disiplin", "Kaynak", "İş kalemi", "Birim", "Miktar", "Birim fiyat"])
        for r in self._all(
                "SELECT i.discipline, c.title, i.description, i.unit, i.contract_qty, i.unit_price, c.project_id FROM pp_items i"
                " JOIN pp_contracts c ON c.id = i.contract_id WHERE c.contractor_party_id = ? ORDER BY c.id, i.line_no",
                party_id):
            t.rows.append(([DISC.get(r["discipline"] or "", ""), f"Sözleşme: {r['title']}", r["description"],
                            r["unit"] or "", _m(r["contract_qty"]), _m(r["unit_price"])],
                           (r["project_id"], "Taşeron Hakedişi")))
        for r in self._all(
                "SELECT k.discipline, k.name, k.project_id, k.id FROM sub_packages k WHERE k.selected_bid_id IN"
                " (SELECT id FROM sub_bids WHERE party_id = ?)", party_id):
            t.rows.append(([DISC.get(r["discipline"] or "", ""), "Kazandığı iş paketi", r["name"], "", "", ""],
                           (r["project_id"], "Alt Yüklenici Teklifleri")))
        out.append(t)

        t = Tab("Hakediş ve ödeme", ["Proje", "Sözleşme", "Hakediş", "Dönem sonu", "Durum"])
        for r in self._all(
                "SELECT c.project_id, pr.name AS project, c.title, p.no, p.revision, p.period_end, p.status FROM pp_payments p"
                " JOIN pp_contracts c ON c.id = p.contract_id LEFT JOIN projects pr ON pr.id = c.project_id"
                " WHERE c.contractor_party_id = ? OR c.employer_party_id = ? ORDER BY p.id DESC", party_id, party_id):
            t.rows.append(([r["project"] or "—", r["title"], f"{r['no']}." + (f" rev.{r['revision']}" if r["revision"] else ""),
                            format_date_tr(r["period_end"]), r["status"]],
                           (r["project_id"], "Taşeron Hakedişi")))
        for r in self._all("SELECT * FROM fin_notes WHERE party_id = ? ORDER BY due_date DESC", party_id):
            t.rows.append((["", f"{r['kind']} {r['number']}".strip(), "", format_date_tr(r["due_date"]),
                            f"{_m(r['amount'])} ₺ · {r['status']}"], (None, "Çek ve Senetler")))
        out.append(t)

        t = Tab("Satın alma hareketleri", ["Proje", "Sipariş", "Tarih", "KDV hariç", "Durum"])
        for r in self._all(
                "SELECT o.id, o.project_id, pr.name AS project, o.order_no, o.order_date, o.status,"
                " (SELECT COALESCE(SUM(CAST(l.quantity AS REAL) * CAST(l.unit_price AS REAL)), 0) FROM po_order_lines l"
                " WHERE l.order_id = o.id) AS total FROM po_orders o JOIN purchase_offers f ON f.id = o.offer_id"
                " LEFT JOIN projects pr ON pr.id = o.project_id WHERE f.party_id = ? ORDER BY o.id DESC", party_id):
            t.rows.append(([r["project"] or "—", r["order_no"], format_date_tr(r["order_date"]),
                            format_tr(Decimal(str(round(r["total"], 2)))), r["status"]],
                           (r["project_id"], "Satın Alma Siparişi ve Teslim")))
        out.append(t)

        out.append(self._performance_tab(party_id))

        t = Tab("Belge ve işlem geçmişi", ["Zaman", "İşlem", "Kayıt", "Not"])
        for r in self._all("SELECT * FROM audit_log WHERE (entity = 'party' AND entity_id = ?) OR (entity = 'cari_entry'"
                           " AND entity_id IN (SELECT id FROM cari_entries WHERE party_id = ?)) ORDER BY id DESC LIMIT 100",
                           party_id, party_id):
            t.rows.append(([str(r["at"])[:16].replace("T", " "), r["action"], r["entity"], r["note"] or ""], None))
        out.append(t)
        return out

    def _performance_tab(self, party_id: int) -> Tab:
        """Alt yüklenici performansı: kayıttan hesaplanan göstergeler; veri yoksa "Veri yok" (puan üretilmez)."""
        from yapimmaliyet.services.performance_service import PerformanceService

        perf = PerformanceService(self.conn).evaluate(party_id)
        t = Tab("Performans", ["Sözleşme", "Gösterge", "Değer", "Dayanak / veri durumu"])
        for c in perf.contracts:
            for i in c.indicators:
                val = f"{format_tr(i.value, 2)} {i.unit}" if i.measured else "Veri yok"
                t.rows.append(([f"{c.project} — {c.title}", i.label, val, i.detail], (c.project_id, "Taşeron Hakedişi")))
        t.rows.append((["", "Kapsam", perf.coverage, "Göstergeler birbirine eklenip puana çevrilmez."], None))
        for n in perf.notes:
            t.rows.append(([format_date_tr(n["rated_at"][:10]), f"Kullanıcı notu: {n['rating']}/5", n["rated_by"], n["note"]], None))
        if perf.average_rating is not None:
            t.rows.append((["", "Not ortalaması (elle girilen)", f"{format_tr(perf.average_rating, 2)} / 5 ({len(perf.notes)} not)", ""], None))
        return t
