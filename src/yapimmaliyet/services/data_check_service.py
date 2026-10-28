"""Veri doğrulama: veritabanı bütünlüğü ve kullanıcıya yol gösteren veri tutarlılığı denetimleri (salt okunur)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from yapimmaliyet.db.connection import check_integrity


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "HATA"      # HATA: veri bozukluğu; UYARI: tamamlanması gereken iş; BILGI


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def run_checks(conn: sqlite3.Connection) -> list[Check]:
    out: list[Check] = []
    problems = check_integrity(conn, quick=False)
    out.append(Check("Veritabanı bütünlüğü ve yabancı anahtarlar", not problems,
                     "Sorun yok." if not problems else "\n".join(problems[:20])))
    n = _count(conn, "SELECT COUNT(*) FROM estimate_items WHERE match_status <> 'ONAYLANDI'")
    out.append(Check("Onaylı eşleşmesi olmayan keşif kalemleri", n == 0,
                     f"{n} kalem eşleşmemiş, önerilmiş ya da ertelenmiş durumda (hesapta fiyatsız sayılır).",
                     "UYARI"))
    rows = conn.execute(
        "SELECT p.name, COUNT(*) FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id"
        " JOIN projects p ON p.id = e.project_id WHERE p.tender_model = 'KARMA' AND i.pricing_method IS NULL"
        " GROUP BY p.id ORDER BY p.name").fetchall()
    out.append(Check("Karma modelde teklif yöntemi atanmamış kalemler", not rows,
                     "Yok." if not rows else "; ".join(f"{r[0]}: {r[1]} kalem" for r in rows), "UYARI"))
    rows = conn.execute(
        "SELECT p.name, COUNT(*) FROM estimate_items i JOIN estimates e ON e.id = i.estimate_id"
        " JOIN projects p ON p.id = e.project_id WHERE i.discipline IS NULL GROUP BY p.id ORDER BY p.name").fetchall()
    out.append(Check("Disiplini atanmamış keşif kalemleri", not rows,
                     "Yok." if not rows else "; ".join(f"{r[0]}: {r[1]} kalem" for r in rows), "BILGI"))
    n = _count(conn, "SELECT COUNT(*) FROM price_books WHERE discipline IS NULL")
    out.append(Check("Disiplini girilmemiş fiyat kitapları", n == 0, f"{n} kitap.", "BILGI"))
    n = _count(conn, "SELECT COUNT(*) FROM price_books WHERE status = 'AKTIF' AND valid_from IS NULL")
    out.append(Check("Yürürlük tarihi girilmemiş aktif kitaplar", n == 0,
                     f"{n} kitap (endeks güncellemesinde temel dönem bulunamaz).", "UYARI"))
    n = _count(conn, "SELECT COUNT(*) FROM estimates e WHERE NOT EXISTS"
                     " (SELECT 1 FROM estimate_items i WHERE i.estimate_id = e.id)")
    out.append(Check("Kalemi olmayan keşif listeleri", n == 0, f"{n} keşif.", "BILGI"))
    n = _count(conn, "SELECT COUNT(*) FROM exchange_rates WHERE source = 'MANUEL'")
    out.append(Check("Elle girilmiş döviz kurları", True, f"{n} kayıt (raporlarda 'elle giriş' olarak işaretlenir).",
                     "BILGI"))
    return out


def as_text(checks: list[Check]) -> tuple[str, str]:
    """(özet, ayrıntılı metin)."""
    errors = [c for c in checks if not c.ok and c.severity == "HATA"]
    warnings = [c for c in checks if not c.ok and c.severity == "UYARI"]
    label = {"HATA": "HATA", "UYARI": "UYARI", "BILGI": "BİLGİ"}
    lines = [f"[{'TAMAM' if c.ok else label[c.severity]}] {c.name}\n    {c.detail.replace(chr(10), chr(10) + '    ')}"
             for c in checks]
    if errors:
        summary = f"{len(errors)} veri bütünlüğü sorunu bulundu. Son yedeği geri yüklemeyi değerlendirin."
    elif warnings:
        summary = f"Veritabanı sağlam. Tamamlanması gereken {len(warnings)} konu var."
    else:
        summary = "Veritabanı sağlam; uyarı yok."
    return summary, "\n\n".join(lines)
