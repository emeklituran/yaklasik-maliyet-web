"""Rapor üretimi: veriyi toplar, rapor modelini oluşturur, Excel ya da PDF'e yazar.

Hesap raporları kesinleşmiş kayıttan (snapshot) üretilir. Kayıt seçilmezse güncel taslak hesap
kullanılır ve rapor her sayfada "TASLAK" olarak işaretlenir.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path

from yapimmaliyet import __version__
from yapimmaliyet.db.repositories import cost_sheets, estimate_items, imports, projects
from yapimmaliyet.domain import analysis
from yapimmaliyet.domain.money import from_db_text
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import AppError, NotFoundError, ValidationError
from yapimmaliyet.reports import builders, estimate_reports, tender_reports
from yapimmaliyet.reports.excel_writer import write_xlsx
from yapimmaliyet.reports.model import ReportDoc
from yapimmaliyet.reports.pdf_writer import write_pdf
from yapimmaliyet.services.calc_service import CalcService
from yapimmaliyet.services.custom_position_service import CustomPositionService
from yapimmaliyet.services.fx_service import FxService
from yapimmaliyet.services.offer_service import OfferService, offer_payload
from yapimmaliyet.services.tender_service import TenderService


class ReportKind(StrEnum):
    KESIF = "KESIF"
    ICMAL = "ICMAL"
    KAYNAK = "KAYNAK"
    ANALIZ = "ANALIZ"
    FIRMA = "FIRMA"
    TEKLIF = "TEKLIF"
    ICE_AKTARMA = "ICE_AKTARMA"
    PROJE = "PROJE"
    EKSIK = "EKSIK"
    KARSILASTIRMA = "KARSILASTIRMA"
    IHALE = "IHALE"
    TEKLIF_CETVELI = "TEKLIF_CETVELI"

    @property
    def label(self) -> str:
        return {"KESIF": "Poz bazlı keşif listesi", "ICMAL": "İş grubu bazlı icmal",
                "KAYNAK": "Birim fiyat kaynak dökümü", "ANALIZ": "Özel poz analiz raporu",
                "FIRMA": "Firma maliyeti raporu", "TEKLIF": "Alternatif teklif karşılaştırması",
                "ICE_AKTARMA": "İçe aktarma hata raporu", "PROJE": "Proje hesaplama özeti",
                "EKSIK": "Eşleşmeyen ve sorunlu kalemler", "KARSILASTIRMA": "Poz birim fiyat karşılaştırması",
                "IHALE": "İhale teklifleri karşılaştırması",
                "TEKLIF_CETVELI": "Teklif cetveli (boş form, ihale dokümanı eki)"}[self.value]


CALC_REPORTS = (ReportKind.KESIF, ReportKind.ICMAL, ReportKind.KAYNAK)
OFFER_REPORTS = (ReportKind.FIRMA, ReportKind.TEKLIF)
ESTIMATE_REPORTS = (ReportKind.EKSIK, ReportKind.KARSILASTIRMA, ReportKind.IHALE, ReportKind.TEKLIF_CETVELI)   # yalnızca keşif seçimi gerektirir


class ReportService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.calc = CalcService(conn)
        self.offer = OfferService(conn)

    # --- veri --------------------------------------------------------------
    def calc_payload(self, estimate_id: int, snapshot_id: int | None, profile_id: int | None) -> dict:
        if snapshot_id is not None:
            p = self.calc.snapshot_payload(snapshot_id)
            if p["kind"] not in ("REFERANS", "YAKLASIK"):
                raise ValidationError("Seçilen kayıt bir yaklaşık maliyet/referans hesabı değil.")
            return {**p, "snapshot_id": snapshot_id}
        if profile_id is None:
            raise ValidationError("Taslak rapor için hesap profili seçin.")
        est = projects.get_estimate(self.conn, estimate_id)
        if est is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        result = self.calc.calculate(estimate_id, profile_id)
        return self.calc.payload(result, self.calc.get_profile(profile_id), est,
                                  projects.get_project(self.conn, est.project_id), None,
                                  FxService(self.conn).estimate_rate(estimate_id))

    def offer_data(self, sheet_id: int | None, snapshot_id: int | None) -> dict:
        if snapshot_id is not None:
            p = self.calc.snapshot_payload(snapshot_id)
            if p["kind"] != "TEKLIF":
                raise ValidationError("Seçilen kayıt bir teklif karşılaştırması değil.")
            return {**p, "snapshot_id": snapshot_id}
        if sheet_id is None:
            raise ValidationError("Maliyet çalışması seçin.")
        sheet = cost_sheets.get_sheet(self.conn, sheet_id)
        est = projects.get_estimate(self.conn, sheet.estimate_id)
        return offer_payload(self.offer.analyse(sheet_id), sheet, est,
                             projects.get_project(self.conn, est.project_id), None, draft=True)

    def build(self, kind: ReportKind, *, estimate_id: int | None = None, snapshot_id: int | None = None,
              profile_id: int | None = None, sheet_id: int | None = None, custom_position_id: int | None = None,
              job_id: int | None = None, project_id: int | None = None) -> ReportDoc:
        if kind in CALC_REPORTS:
            p = self.calc_payload(estimate_id, snapshot_id, profile_id)
            return {ReportKind.KESIF: builders.estimate_list, ReportKind.ICMAL: builders.group_summary,
                    ReportKind.KAYNAK: builders.price_sources}[kind](p)
        if kind in OFFER_REPORTS:
            p = self.offer_data(sheet_id, snapshot_id)
            return builders.firm_cost(p) if kind is ReportKind.FIRMA else builders.offer_comparison(p)
        if kind is ReportKind.ANALIZ:
            svc = CustomPositionService(self.conn)
            cp = svc.get(custom_position_id)
            entries = []
            for v in svc.versions(cp.id):
                spec = svc.analysis_of(v.id)
                entries.append((v, spec, analysis.compute(spec) if spec else None))
            return builders.analysis_report(cp, entries)
        if kind is ReportKind.ICE_AKTARMA:
            job = next((j for j in imports.list_jobs(self.conn, 10_000) if j.id == job_id), None)
            if job is None:
                raise NotFoundError("İçe aktarma işi bulunamadı.")
            return builders.import_issues(job, imports.job_issues(self.conn, job_id))
        if kind is ReportKind.PROJE:
            project = projects.get_project(self.conn, project_id)
            if project is None:
                raise NotFoundError("Proje bulunamadı.")
            ests = projects.list_estimates(self.conn, project_id)
            snaps = [(s, self.calc.snapshot_payload(s.id)) for e in ests for s in self.calc.snapshots(e.id)]
            return builders.project_summary(project, ests, snaps)
        if kind in ESTIMATE_REPORTS:
            return self._estimate_report(kind, estimate_id)
        raise ValidationError(f"Bilinmeyen rapor türü: {kind}")

    def _estimate_report(self, kind: ReportKind, estimate_id: int | None) -> ReportDoc:
        est = projects.get_estimate(self.conn, estimate_id) if estimate_id is not None else None
        if est is None:
            raise ValidationError("Rapor için keşif seçin.")
        project = projects.get_project(self.conn, est.project_id)
        items = estimate_items.list_items(self.conn, estimate_id)
        if kind is ReportKind.EKSIK:
            return estimate_reports.match_issues(project, est, items, __version__)
        if kind is ReportKind.IHALE:
            view, cmp = TenderService(self.conn).compare(estimate_id)
            return tender_reports.bid_comparison(view, cmp, __version__)
        if kind is ReportKind.TEKLIF_CETVELI:
            try:
                return tender_reports.offer_schedule(TenderService(self.conn).view(estimate_id), __version__)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        codes = {normalize_code(i.code) for i in items if i.code}
        prices: dict[str, list] = {}
        books: dict[int, str] = {}
        for r in self.conn.execute(
                "SELECT p.code_norm, pp.unit_price, pb.id, pb.title, pb.year, pb.edition,"
                " COALESCE(a.short_name, a.name) AS auth FROM positions p"
                " JOIN position_prices pp ON pp.position_id = p.id JOIN price_books pb ON pb.id = pp.price_book_id"
                " JOIN authorities a ON a.id = pb.authority_id WHERE pb.status <> 'TASLAK'"
                " ORDER BY pb.year, pb.title, pb.edition"):
            if r["code_norm"] in codes:
                prices.setdefault(r["code_norm"], []).append((r["id"], from_db_text(r["unit_price"])))
                books.setdefault(r["id"], f"{r['auth']} {r['title']} {r['year']} {r['edition']}".strip())
        return estimate_reports.price_comparison(project, est, items, prices, list(books.items()), __version__)

    def export(self, doc: ReportDoc, path: Path | str) -> Path:
        path = Path(path)
        suffix = path.suffix.lower()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if suffix == ".xlsx":
                return write_xlsx(doc, path)
            if suffix == ".pdf":
                return write_pdf(doc, path)
        except PermissionError as exc:
            raise AppError("Rapor dosyası yazılamadı. Dosya başka bir programda (ör. Excel) açık olabilir; "
                           "kapatıp yeniden deneyin.", detail=str(exc)) from exc
        except OSError as exc:
            raise AppError("Rapor dosyası yazılamadı (disk dolu ya da klasör yazmaya kapalı olabilir).",
                           detail=str(exc)) from exc
        raise ValidationError("Rapor yalnızca .xlsx ya da .pdf olarak kaydedilebilir.")
