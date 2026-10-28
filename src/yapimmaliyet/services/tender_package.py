"""İhale / teklif dosyası paketi: bir klasöre istekliye verilecek boş teklif cetveli, idare içi yaklaşık maliyet
belgeleri ve (seçilirse) doldurulmuş teklif cetveli yazılır; içindekiler listesi SHA-256 özetleriyle oluşturulur.

İdare içi belgeler (yaklaşık maliyet hesap cetveli, icmal) ayrı alt klasöre konur ve "GİZLİ" notu taşır:
4734 sayılı Kanun md. 9'a göre yaklaşık maliyet ihale dokümanında açıklanmaz.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yapimmaliyet import __version__
from yapimmaliyet.db.repositories import tender_bids
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.reports.tender_reports import offer_schedule
from yapimmaliyet.services.calc_service import CalcService
from yapimmaliyet.services.report_service import ReportKind, ReportService
from yapimmaliyet.services.tender_service import TenderService

CONFIDENTIAL = ("GİZLİ — İDARE İÇİ BELGE: Yaklaşık maliyet, 4734 sayılı Kanun md. 9 gereğince ihale dokümanında "
                "açıklanmaz; isteklilere verilmez.")


@dataclass(frozen=True)
class PackageResult:
    folder: Path
    files: list[Path]
    index: Path


def _slug(text: str) -> str:
    s = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_")
    return s[:40] or "teklif"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_package(conn: sqlite3.Connection, estimate_id: int, folder: Path | str, bid_id: int | None = None,
                  profile_id: int | None = None) -> PackageResult:
    tender, reports, calc = TenderService(conn), ReportService(conn), CalcService(conn)
    view = tender.view(estimate_id, profile_id)
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ValidationError(f"Klasör boş değil; mevcut dosyaların üzerine yazılmaz: {folder}")
    (folder / "idare_ici_GIZLI").mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    def save(doc, rel: str) -> None:
        for ext in ("pdf", "xlsx"):
            files.append(reports.export(doc, folder / f"{rel}.{ext}"))

    save(offer_schedule(view, __version__), "01_teklif_cetveli_BOS_FORM")
    # en son kesinleşmiş yaklaşık maliyet (yoksa referans) kaydı; liste yeniden eskiye sıralıdır
    snaps = sorted((s for s in calc.snapshots(estimate_id) if s.kind in ("YAKLASIK", "REFERANS")),
                   key=lambda s: (s.kind != "YAKLASIK", -s.id))
    kw = {"snapshot_id": snaps[0].id} if snaps else {"estimate_id": estimate_id,
                                                       "profile_id": profile_id or tender.default_profile()}
    for kind, rel in ((ReportKind.KESIF, "idare_ici_GIZLI/02_yaklasik_maliyet_hesap_cetveli"),
                      (ReportKind.ICMAL, "idare_ici_GIZLI/03_icmal")):
        doc = reports.build(kind, **kw)
        doc.notes.insert(0, CONFIDENTIAL)
        save(doc, rel)
    bid = None
    if bid_id is not None:
        bid = tender_bids.get_bid(conn, bid_id)
        if bid is None or bid.estimate_id != estimate_id:
            raise NotFoundError("Teklif bu keşfe ait değil.")
        save(offer_schedule(view, __version__, bid, tender.bid_lines(bid_id)), f"04_teklif_cetveli_{_slug(bid.bidder)}")
    lines = [f"İhale / teklif dosyası — {view.project.name}",
             f"Oluşturma: {datetime.now():%d.%m.%Y %H:%M} · YapımMaliyet {__version__}",
             f"Keşif: {view.estimate.display_name} · İhale modeli: {view.summary.model_label}",
             "Yaklaşık maliyet kaynağı: " + (f"kesinleşmiş kayıt No {snaps[0].id}" if snaps
                                            else "TASLAK hesap (kesinleşmemiş)"),
             "" if bid is None else f"Teklif: {bid.bidder}" + (" (ÖRNEK / sentetik)" if bid.is_sample else ""),
             "", "Dosyalar (SHA-256):"]
    lines += [f"  {p.relative_to(folder).as_posix()}  {_sha(p)}" for p in files]
    lines += ["", "01: istekliye verilecek boş form (ihale dokümanı eki).",
              "idare_ici_GIZLI/: yaklaşık maliyet belgeleri; isteklilere verilmez (4734 md. 9).",
              "04: doldurulmuş teklif cetveli taslağı; ihale dokümanındaki standart form esas alınmalıdır."]
    index = folder / "ICINDEKILER.txt"
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return PackageResult(folder, files, index)
