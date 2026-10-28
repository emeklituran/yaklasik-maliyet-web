"""Paketlenmiş programın kendi kendini doğrulaması (`YapimMaliyet.exe --self-test sonuc.json`).

Arayüz açmadan, geçici bir veri klasöründe temel bileşenleri dener ve sonucu JSON olarak yazar.
Paketin temiz bir makinede gerçekten çalıştığını kanıtlamak için kullanılır.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import traceback
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from yapimmaliyet import __version__


def _check(results: dict, name: str, fn: Callable[[], object]) -> None:
    try:
        detail = fn()
        results["checks"][name] = {"ok": True, "detail": detail}
    except Exception as exc:  # her denetim bağımsızdır; hata kaydedilip devam edilir
        results["checks"][name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}",
                                   "trace": traceback.format_exc(limit=3)}


def run_self_test(work_dir: Path | None = None) -> dict:
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="ym_selftest_"))
    work.mkdir(parents=True, exist_ok=True)
    results: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
                     "executable": sys.executable, "work_dir": str(work), "checks": {}}
    ctx_holder: dict = {}

    def bootstrap_db():
        from yapimmaliyet.services.app_context import bootstrap

        ctx = bootstrap(work / "Veri ĞÜŞ test")
        ctx.settings.backup_on_exit = False
        ctx_holder["ctx"] = ctx
        return {"schema": ctx.schema_version, "data_dir": str(ctx.paths.data_dir)}

    def fts5():
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return sqlite3.sqlite_version

    def calc():
        from yapimmaliyet.domain.rounding import line_amount

        assert line_amount(Decimal("412.500"), Decimal("3120.00")) == Decimal("1287000.00")
        return "ok"

    def ocr():
        import pymupdf

        from yapimmaliyet.fileio import pdf_ocr

        info = pdf_ocr.require_tesseract()
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=200)
        page.insert_text((40, 100), "POZ 15.150.1001  3.120,00", fontsize=20)
        pix = page.get_pixmap(dpi=200)
        scanned = pymupdf.open()
        sp = scanned.new_page(width=400, height=200)
        sp.insert_image(sp.rect, pixmap=pix)
        path = work / "ocr_test.pdf"
        scanned.save(path)
        table = pdf_ocr.ocr_pdf(path, [1])
        text = " ".join(str(v) for r in table.rows for v in r.values if v)
        assert "15.150.1001" in text, text
        return {"tesseract": str(info.exe), "tessdata": str(info.tessdata), "text": text}

    def reports():
        from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table
        from yapimmaliyet.services.report_service import ReportService

        doc = ReportDoc("Öz Sınama Raporu — ğüşıöçİ", [("Sürüm", __version__)],
                        [Section("Tablo", Table([Col("Ad"), Col("Tutar", ColKind.MONEY)], [["Şap", Decimal("1234.5")]]))])
        rs = ReportService(ctx_holder["ctx"].conn)
        x = rs.export(doc, work / "rapor.xlsx")
        p = rs.export(doc, work / "rapor.pdf")
        return {"xlsx": x.stat().st_size, "pdf": p.stat().st_size}

    def backup():
        ctx = ctx_holder["ctx"]
        path = ctx.backup.create_backup(ctx.conn, "oz_sinama")
        return {"backup": path.name, "schema": ctx.backup.verify_backup(path)}

    def qt():
        from PySide6.QtCore import QLibraryInfo

        return QLibraryInfo.version().toString()

    for name, fn in (("veritabani", bootstrap_db), ("fts5", fts5), ("hesap", calc), ("ocr", ocr),
                     ("raporlar", reports), ("yedekleme", backup), ("qt", qt)):
        _check(results, name, fn)
    if "ctx" in ctx_holder:
        from yapimmaliyet.services.app_context import shutdown

        shutdown(ctx_holder["ctx"])
    results["ok"] = all(c["ok"] for c in results["checks"].values())
    return results


def main(out_path: str) -> int:
    results = run_self_test()
    Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if results["ok"] else 1
