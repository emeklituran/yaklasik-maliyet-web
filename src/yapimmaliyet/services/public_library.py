"""Kamu birim fiyat kütüphanesi: kurum kataloğu, doğrulanmış başlangıç kayıtları, kurum durumu ve kaynak paketi aktarımı.

- Katalog: kurumların resmî kaynak tanımı (bağlantı, kapsam, duyurulan son dönem). Mevcut idare kaydı adıyla ya da kısa
  adıyla eşleşirse ona bağlanır; ikinci idare kaydı açılmaz. Poz kodu kurumla birlikte benzersizdir.
- Başlangıç kayıtları: yalnız kaynakla tek tek karşılaştırılmış fiyatlar (ÇŞB 2026, 8 kayıt). Sürüm işaretiyle bir kez
  kurulur; ikinci çalıştırma mükerrer kayıt üretmez, kullanıcı silerse geri getirilmez. Kullanıcı verisine dokunulmaz.
- Kaynak paketi: kullanıcının dosyası salt-okunur açılır; poz kodu adayları kontrol listesine yazılır, fiyat kitaplarına
  yazılmaz. Resmî kitapların sayfa metni alınmaz (yeniden dağıtım izni belirsiz).
- Kurum durumu kayıtlardan hesaplanır; aktarılmamış kurum hazır gösterilmez.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, positions, price_books
from yapimmaliyet.domain.models import BookStatus, NewPositionPrice
from yapimmaliyet.domain.money import to_db_text
from yapimmaliyet.domain.periods import UNVERIFIED, period_from_catalog
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search
from yapimmaliyet.infra.errors import NotFoundError, ValidationError

SEED_KEY = "public_library_seed"
SEED_VERSION = 1


def seed_enabled() -> bool:
    """Testler ortam değişkeniyle kapatır (YAPIMMALIYET_TOHUM=0); program her zaman açık çalışır."""
    return os.environ.get("YAPIMMALIYET_TOHUM", "1") != "0"


@dataclass(frozen=True)
class CatalogEntry:
    code: str
    name: str
    short: str
    url: str
    scope: str
    period: str
    note: str
    aliases: tuple[str, ...]


# Kaynak: YapımMaliyet Kamu Kaynak Paketi 2026 (kurum_kaynaklari_2026.csv, 27.10.2026 incelemesi).
CATALOG = (
    CatalogEntry("CSB", "Çevre, Şehircilik ve İklim Değişikliği Bakanlığı / YFK", "ÇŞB",
                 "https://yfk.csb.gov.tr/birim-fiyatlar-100468", "İnşaat, mekanik, elektrik; rayiç ve analiz", "2026-01",
                 "2026-10 aylık değişiklikleri ayrıca yüklenmeli.",
                 ("csb", "cevre sehircilik", "yuksek fen kurulu", "yfk")),
    CatalogEntry("KGM", "Karayolları Genel Müdürlüğü", "KGM",
                 "https://www.kgm.gov.tr/SiteCollectionDocuments/KGMdocuments/MerkezBirimler/"
                 "ProgramIzlemeDairesiBaskanligi/2026YiliBirimFiyatListesi.pdf", "Yol, köprü, tünel, trafik", "2026",
                 "Aylık ek listeler kontrol edilmeli.", ("kgm", "karayollari")),
    CatalogEntry("DSI", "Devlet Su İşleri Genel Müdürlüğü", "DSİ", "https://www.dsi.gov.tr/Sayfa/Detay/738",
                 "Su yapıları, baraj, sulama", "2026", "Analiz ve tarifler ayrı belgelerde.", ("dsi", "devlet su isleri")),
    CatalogEntry("ILBANK", "İller Bankası A.Ş.", "İLBANK", "https://www.ilbank.gov.tr/raporlar/bankamiz-birim-fiyat-cetvelleri/24",
                 "Altyapı, jeoteknik, harita, sondaj, imar", "2026-01", "Aylık cetveller ve alt analizler.",
                 ("ilbank", "iller bankasi")),
    CatalogEntry("MSB", "Millî Savunma Bakanlığı", "MSB",
                 "https://www.msb.gov.tr/FiyatMaliyet/icerik/ac97291533814ba9b70616cc02a4facd",
                 "İnşaat fiyat ve analizleri", "2026", "Belge indirilmeli.", ("msb", "milli savunma")),
    CatalogEntry("KVMGM", "Kültür Varlıkları ve Müzeler Genel Müdürlüğü", "KVMGM",
                 "https://kvmgm.ktb.gov.tr/TR-447694/2026-yili-birim-fiyat-listeleri-birim-fiyat-analiz-ve-t-.html",
                 "Restorasyon ve koruma", "2026", "VGM ile ortak yayın; aynı belge iki kez aktarılmamalı.",
                 ("kvmgm", "kultur varliklari", "kultur ve turizm")),
    CatalogEntry("VGM", "Vakıflar Genel Müdürlüğü", "VGM",
                 "https://kvmgm.ktb.gov.tr/TR-447694/2026-yili-birim-fiyat-listeleri-birim-fiyat-analiz-ve-t-.html",
                 "Restorasyon, vakıf eserleri", "2026", "KVMGM ile ortak belge.", ("vgm", "vakiflar")),
    CatalogEntry("TEDAS", "Türkiye Elektrik Dağıtım A.Ş.", "TEDAŞ", "https://birimfiyat.tedas.gov.tr/",
                 "Elektrik dağıtım: malzeme, montaj, demontaj", "2026",
                 "Malzeme, montaj, demontaj ve demontajdan montaj ayrı fiyat türleridir.", ("tedas", "elektrik dagitim")),
    CatalogEntry("AYGM", "Altyapı Yatırımları Genel Müdürlüğü", "AYGM", "https://aygm.uab.gov.tr/birim-fiyatlar",
                 "Altyapı, etüt, laboratuvar", "2026-1.donem", "Dönemli fiyat.",
                 ("aygm", "altyapi yatirimlari", "ulastirma ve altyapi")),
    CatalogEntry("PTT", "Posta ve Telgraf Teşkilatı A.Ş.", "PTT",
                 "https://www.ptt.gov.tr/duyurular/202609-ptt-as-yapi-daire-baskanligi-ozel-birim-fiyatlari-rayicleri-"
                 "ve-tuik-endeksi-eslestirme-kodlari-93e35c75284846ddb28a7fa2c83fcad8", "Yapı özel poz ve rayiç",
                 "2026-09", "Aylık özel fiyatlar.", ("ptt", "posta ve telgraf")),
    CatalogEntry("OGM", "Orman Genel Müdürlüğü", "OGM",
                 "https://www.ogm.gov.tr/tr/duyurular/agaclandirma-rehabilitasyon-toprak-muhafaza-erozyon-ve-sel-kontrolu-"
                 "mera-islahi-fidanlik-silvikultur-ve-etut-proje-is-2", "Ağaçlandırma, erozyon, silvikültür", "2026",
                 "Resmî duyuru; ikincil kaynak.", ("ogm", "orman genel")),
    CatalogEntry("CEM", "Çevre Yönetimi Genel Müdürlüğü (ÇŞB)", "ÇYGM", "https://cem.csb.gov.tr/birim-fiyatlar-113710",
                 "Çevre yönetimi", "2026", "ÇŞB alt birimi; 2026 yayını ayrıca doğrulanmalı.",
                 ("cevre yonetimi", "cygm")),
)

# ÇŞB 2026: Birim Fiyat ve Tarifleri (PDF s.326, basılı 305) "Dikişli Borular; Vidalı TS EN 10255+A1'e uygun (malzemesi
# Fe.33)" tablosu ve Mekanik Tesisat Birim Fiyat Analizleri Cilt 2 (analiz tarihi 01.01.2026). Fiyat = malzeme + montaj
# birim fiyatı, montaj = montaj bedeli. Her satır iki belgede elle karşılaştırıldı.
_PIPE = "Dikişli Borular; Vidalı TS EN 10255+A1'e uygun (malzemesi Fe.33)"
SEED_PRICES = (
    # kod, anma (inç), anma (mm), dış çap/et, kg/m, birim fiyat, montaj bedeli, Cilt 2 PDF sayfası, basılı sayfa
    ("25.300.1101", '1/2"', "15", "21,3/2,60", "1,22", "181.50", "126.88", 2, "805"),
    ("25.300.1102", '3/4"', "20", "26,9/2,60", "1,57", "230.63", "160.63", 2, "805"),
    ("25.300.1103", '1"', "25", "33,7/3,20", "2,43", "298.75", "194.38", 3, "806"),
    ("25.300.1104", '1 1/4"', "32", "42,4/3,20", "3,13", "425.00", "287.50", 3, "806"),
    ("25.300.1105", '1 1/2"', "40", "48,3/3,20", "3,60", "480.00", "321.25", 4, "807"),
    ("25.300.1106", '2"', "50", "60,3/3,60", "5,10", "578.75", "355.00", 4, "807"),
    ("25.300.1107", '2 1/2"', "65", "76,1/3,60", "6,54", "646.25", "355.00", 5, "808"),
    ("25.300.1108", '3"', "80", "88,9/4,00", "8,53", "768.75", "388.75", 5, "808"),
)
SEED_BOOK = dict(title="Mekanik Tesisat Birim Fiyatları — doğrulanmış başlangıç kayıtları", year=2026,
                 edition="Başlangıç", period="2026 Ocak", valid_from="2026-01-01", price_kind="MALZEME_MONTAJ",
                 source_ref="Mekanik Tesisat Birim Fiyat Analizleri Cilt 2 (s. 805–808); Birim Fiyat ve Tarifleri 2026 "
                            "(s. 305)")

STATUS_DEFINED = "Kaynak tanımlandı"
STATUS_WAITING = "Dosya bekleniyor"
STATUS_IMPORTED = "İçe aktarıldı"
STATUS_CHECK = "Kontrol gerekiyor"
STATUS_VERIFIED = "Doğrulandı"
STATUS_UPDATE = "Güncelleme mevcut"


@dataclass(frozen=True)
class InstitutionStatus:
    authority_id: int
    code: str | None
    name: str
    url: str | None
    catalog_period: str | None
    status: str
    books: int
    active_books: int
    prices: int
    verified_prices: int
    candidates: int
    latest_period: str | None
    note: str | None


@dataclass(frozen=True)
class PackageReport:
    sha256: str
    institutions_added: int
    institutions_linked: int
    candidates_added: int
    candidates_existing: int
    verified_skipped: int      # zaten doğrulanmış kitapta bulunan fiyat örnekleri (aday yapılmaz)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _catalog_key(period: str | None) -> tuple[int, int]:
    """Katalog dönemi → (yıl, ay); ay bilinmiyorsa 0."""
    if not period or not period[:4].isdigit():
        return (0, 0)
    month = period[5:7]
    return (int(period[:4]), int(month) if month.isdigit() else 0)


class PublicLibraryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- katalog ---------------------------------------------------------------------------------------
    def _match_existing(self, entry: CatalogEntry) -> int | None:
        for r in self.conn.execute("SELECT id, name, short_name, code FROM authorities"):
            if r["code"] == entry.code:
                return r["id"]
        def hit(key: str, alias: str) -> bool:   # kısa takma ad yalnız tam kelime olarak eşleşir ("ogm", "ptt")
            return alias in key.split() if len(alias) <= 6 else alias in key

        for r in self.conn.execute("SELECT id, name, short_name FROM authorities WHERE code IS NULL"):
            keys = {normalize_search(r["name"]), normalize_search(r["short_name"] or "")}
            if any(k and (k == normalize_search(entry.short) or k == entry.code.lower() or
                          any(hit(k, a) for a in entry.aliases)) for k in keys):
                return r["id"]
        return None

    def _ensure_entry(self, entry: CatalogEntry) -> tuple[int, bool]:
        """Kurumu katalog bilgisiyle bağlar ya da ekler. Döndürür: (idare id, yeni mi)."""
        aid = self._match_existing(entry)
        if aid is None:
            name = entry.name
            if self.conn.execute("SELECT 1 FROM authorities WHERE name = ?", (name,)).fetchone():
                name = f"{name} ({entry.code})"
            aid = self.conn.execute("INSERT INTO authorities(name, short_name, notes) VALUES (?, ?, ?)",
                                    (name, entry.short, "Kamu kaynak kataloğundan eklendi.")).lastrowid
            created = True
        else:
            created = False
        self.conn.execute("UPDATE authorities SET code = ?, url = ?, scope = ?, catalog_period = ?, catalog_note = ?"
                          " WHERE id = ?", (entry.code, entry.url, entry.scope, entry.period, entry.note, aid))
        return aid, created

    def ensure_catalog(self, entries=CATALOG) -> dict[str, int]:
        with transaction(self.conn):
            return {e.code: self._ensure_entry(e)[0] for e in entries}

    # --- başlangıç kayıtları ---------------------------------------------------------------------------
    def seed_version(self) -> int:
        r = self.conn.execute("SELECT value FROM app_meta WHERE key = ?", (SEED_KEY,)).fetchone()
        return int(r[0]) if r and str(r[0]).isdigit() else 0

    def ensure_seed(self) -> bool:
        """Katalog ve doğrulanmış başlangıç kayıtlarını bir kez kurar. Döndürür: kurulum yapıldı mı."""
        if self.seed_version() >= SEED_VERSION:
            return False
        with transaction(self.conn):
            ids = {e.code: self._ensure_entry(e)[0] for e in CATALOG}
            aid = ids["CSB"]
            b = SEED_BOOK
            if not price_books.exists(self.conn, aid, b["year"], b["edition"]):
                book_id = price_books.insert(self.conn, aid, b["title"], b["year"], b["edition"], b["valid_from"],
                                             "Program ile gelen başlangıç kütüphanesi: yalnız kaynakla karşılaştırılmış "
                                             "kayıtlar. Tam kitap için resmî PDF'i içe aktarın.")
                price_books.set_meta(self.conn, book_id, period=b["period"], price_kind=b["price_kind"],
                                     verification="DOGRULANDI", source_ref=b["source_ref"])
                price_books.set_discipline(self.conn, book_id, "MEKANIK")
                items = [NewPositionPrice(code, f'{_PIPE} — {inch} ({mm} mm; dış çap/et kalınlığı {od} mm; {kg} kg/m)',
                                          "m", Decimal(price), "25.300 Dikişli çelik borular", page, None,
                                          f"Cilt 2 PDF s.{page} (basılı {printed}); Birim Fiyat ve Tarifleri 2026 PDF "
                                          "s.326 (basılı 305)", None, Decimal(montage), printed)
                         for code, inch, mm, od, kg, price, montage, page, printed in SEED_PRICES]
                positions.insert_prices(self.conn, aid, book_id, items)
                price_books.set_status(self.conn, book_id, BookStatus.AKTIF)
                audit.log(self.conn, "price_book", book_id, "EKLE", new={"seed": SEED_VERSION, "prices": len(items)},
                          note="Kamu kütüphanesi başlangıç kayıtları")
            self.conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (SEED_KEY, str(SEED_VERSION)))
        return True

    # --- kurum durumu ----------------------------------------------------------------------------------
    def statuses(self) -> list[InstitutionStatus]:
        out = []
        rows = self.conn.execute(
            "SELECT a.* FROM authorities a WHERE a.code IS NOT NULL OR EXISTS (SELECT 1 FROM price_books pb"
            " WHERE pb.authority_id = a.id AND pb.hidden = 0) ORDER BY a.code IS NULL, a.name").fetchall()
        for a in rows:
            books = self.conn.execute(
                "SELECT pb.status, pb.verification, pb.year, pb.valid_from, pb.period,"
                " (SELECT COUNT(*) FROM position_prices pp WHERE pp.price_book_id = pb.id) AS n"
                " FROM price_books pb WHERE pb.authority_id = ? AND pb.hidden = 0", (a["id"],)).fetchall()
            cand = self.conn.execute("SELECT COUNT(*) FROM code_candidates WHERE authority_id = ? AND status = 'ADAY'",
                                     (a["id"],)).fetchone()[0]
            active = [b for b in books if b["status"] == "AKTIF" and b["verification"] not in UNVERIFIED]
            verified = sum(b["n"] for b in active if b["verification"] == "DOGRULANDI")
            if not books and not cand:
                status = STATUS_WAITING if a["url"] else STATUS_DEFINED
            elif not active:
                status = STATUS_CHECK
            else:
                status = STATUS_VERIFIED if any(b["verification"] == "DOGRULANDI" for b in active) else STATUS_IMPORTED
                latest = max(((b["year"], int(b["valid_from"][5:7]) if b["valid_from"] else 0) for b in active))
                cat = _catalog_key(a["catalog_period"])
                if cat[0] > latest[0] or (cat[0] == latest[0] and cat[1] > latest[1] > 0):
                    status = STATUS_UPDATE
            latest_book = max(books, key=lambda b: (b["year"], b["valid_from"] or ""), default=None)
            latest_period = (latest_book["period"] or str(latest_book["year"])) if latest_book else None
            out.append(InstitutionStatus(a["id"], a["code"], a["name"], a["url"], period_from_catalog(a["catalog_period"]),
                                         status, len(books), len(active), sum(b["n"] for b in books), verified, cand,
                                         latest_period, a["catalog_note"]))
        return out

    # --- kaynak paketi ---------------------------------------------------------------------------------
    def import_package(self, path: Path | str) -> PackageReport:
        """Kamu kaynak paketini (SQLite) içe aktarır: katalog + poz kodu adayları (kontrol listesi). Tek transaction;
        aynı paket ikinci kez aktarılırsa yeni kayıt oluşmaz. Paket dosyasına yazılmaz (immutable açılır)."""
        path = Path(path)
        if not path.is_file():
            raise NotFoundError(f"Dosya bulunamadı: {path}")
        if zipfile.is_zipfile(path):   # indirilen ZIP: yalnız içindeki .sqlite okunur, başka dosya açılmaz
            with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
                members = [m for m in z.infolist() if m.filename.lower().endswith(".sqlite") and not m.is_dir()]
                if len(members) != 1:
                    raise ValidationError("ZIP içinde tek bir kaynak paketi veritabanı (.sqlite) bulunmalı.")
                target = Path(tmp) / "paket.sqlite"
                target.write_bytes(z.read(members[0]))
                return self.import_package(target)
        sha = _sha256(path)
        try:
            src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            src.row_factory = sqlite3.Row
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as exc:
            raise ValidationError("Dosya bir kaynak paketi veritabanı değil ya da bozuk.") from exc
        try:
            need = {"kaynak_katalogu", "poz_indeksi", "belgeler"}
            if not need <= tables:
                raise ValidationError("Kaynak paketi tanınmadı: gerekli tablolar yok "
                                      f"({', '.join(sorted(need - tables))}).")
            return self._import_package(src, tables, sha)
        except sqlite3.DatabaseError as exc:
            raise ValidationError("Kaynak paketi okunamadı; dosya bozuk olabilir.") from exc
        finally:
            src.close()

    def _import_package(self, src: sqlite3.Connection, tables: set[str], sha: str) -> PackageReport:
        known = {e.code: e for e in CATALOG}
        added = linked = new_c = old_c = skipped = 0
        with transaction(self.conn):
            ids: dict[str, int] = {}
            for r in src.execute("SELECT * FROM kaynak_katalogu"):
                code = (r["kurum_kodu"] or "").strip().upper()
                if not code:
                    continue
                base = known.get(code)
                entry = CatalogEntry(code, base.name if base else (r["kurum_adi"] or code), base.short if base else code,
                                     r["resmi_url"] or (base.url if base else ""), base.scope if base else
                                     (r["kapsam"] or ""), r["donem"] or (base.period if base else ""),
                                     base.note if base else (r["notlar"] or ""), base.aliases if base else ())
                ids[code], created = self._ensure_entry(entry)
                added, linked = added + created, linked + (not created)
            periods = {c: self.conn.execute("SELECT catalog_period FROM authorities WHERE id = ?", (i,)).fetchone()[0]
                       for c, i in ids.items()}
            docs = {r["id"]: r for r in src.execute("SELECT * FROM belgeler")}
            before = self.conn.execute("SELECT COUNT(*) FROM code_candidates").fetchone()[0]
            rows = []
            for r in src.execute("SELECT belge_id, sayfa, poz_kodu, aciklama_onizleme, kayit_turu FROM poz_indeksi"):
                doc = docs.get(r["belge_id"])
                code = (doc["kurum"] or "").upper() if doc else ""
                if not doc or code not in ids or not normalize_code(r["poz_kodu"] or ""):
                    continue
                kind = "ANALIZ_POZ" if "ANALIZ" in (r["kayit_turu"] or "") else "KITAP_KOD"
                rows.append((ids[code], r["poz_kodu"].strip(), normalize_code(r["poz_kodu"]), kind,
                             (r["aciklama_onizleme"] or "")[:300], None, None, period_from_catalog(periods.get(code)),
                             doc["dosya_adi"], doc["sha256"] or sha, r["sayfa"]))
            if "dogrulanmis_fiyatlar" in tables:
                for r in src.execute("SELECT * FROM dogrulanmis_fiyatlar"):
                    code = (r["kurum_kodu"] or "").upper()
                    if code not in ids:
                        continue
                    exists = self.conn.execute(
                        "SELECT 1 FROM position_prices pp JOIN positions p ON p.id = pp.position_id JOIN price_books pb"
                        " ON pb.id = pp.price_book_id WHERE p.authority_id = ? AND p.code_norm = ?"
                        " AND pb.verification = 'DOGRULANDI'", (ids[code], normalize_code(r["poz_kodu"]))).fetchone()
                    if exists:
                        skipped += 1
                        continue
                    price = to_db_text(Decimal(r["fiyat_kurus"]) / 100, 2)
                    rows.append((ids[code], r["poz_kodu"], normalize_code(r["poz_kodu"]), "FIYAT", r["dayanak"],
                                 r["birim"], price, period_from_catalog(r["donem"]), r["dayanak"], sha, r["pdf_sayfa"]))
            self.conn.executemany(
                "INSERT OR IGNORE INTO code_candidates(authority_id, code, code_norm, kind, preview, unit, price, period,"
                " document_name, document_sha, page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            new_c = self.conn.execute("SELECT COUNT(*) FROM code_candidates").fetchone()[0] - before
            old_c = len(rows) - new_c
            audit.log(self.conn, "code_candidate", None, "EKLE", new={"package_sha": sha, "added": new_c},
                      note="Kamu kaynak paketi içe aktarıldı")
        return PackageReport(sha, added, linked, new_c, old_c, skipped)

    # --- kontrol listesi --------------------------------------------------------------------------------
    def candidates(self, authority_id: int | None = None, text: str = "", status: str | None = "ADAY",
                   limit: int = 500) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, a.name AS authority_name FROM code_candidates c JOIN authorities a ON a.id = c.authority_id"
               " WHERE 1 = 1")
        params: list = []
        if authority_id is not None:
            sql += " AND c.authority_id = ?"
            params.append(authority_id)
        if status:
            sql += " AND c.status = ?"
            params.append(status)
        if text.strip():
            sql += " AND (c.code_norm LIKE ? OR c.preview LIKE ?)"
            params += [f"%{normalize_code(text)}%", f"%{text.strip()}%"]
        sql += " ORDER BY c.code_norm, c.page LIMIT ?"
        return self.conn.execute(sql, [*params, limit]).fetchall()

    def candidate_count(self, status: str | None = "ADAY") -> int:
        sql, params = "SELECT COUNT(*) FROM code_candidates", []
        if status:
            sql, params = sql + " WHERE status = ?", [status]
        return self.conn.execute(sql, params).fetchone()[0]

    def set_candidate_status(self, ids: list[int], status: str) -> None:
        if status not in ("ADAY", "INCELENDI", "REDDEDILDI"):
            raise ValidationError("Aday durumu geçersiz.")
        with transaction(self.conn):
            self.conn.executemany("UPDATE code_candidates SET status = ? WHERE id = ?", [(status, i) for i in ids])
