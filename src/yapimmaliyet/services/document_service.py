"""Doküman ve Onay Merkezi: teknik belge / malzeme onayı kayıtları, sürümler (ek dosya), karar ve gerekçe.

Kurallar:
- Her dokümanın bir projesi vardır; isteğe bağlı olarak sözleşme, sipariş, satın alma talebi ya da alt yüklenici paketine
  ve bir firmaya bağlanır (bağlantı aynı projeye ait olmalıdır).
- Ek dosya veri klasörüne kopyalanır (`KaynakDosyalar/Dokumanlar/<sha256><uzantı>`), özeti saklanır; aynı dosya tek kez durur.
  `verify_file` dosyanın özetini yeniden hesaplar (değişmiş ya da kaybolmuşsa bildirir).
- Akış: TASLAK → (incelemeye gönder) INCELEMEDE → karar: ONAY / RED / REVİZYON (gerekçe zorunlu, karar veren kayıtlı).
  Karar en son sürüme verilir. Yeni sürüm eklenince doküman TASLAK'a döner ve yeniden incelemeye gönderilmelidir;
  önceki onay geçmişte kalır, onaylı sürümün dosyası değiştirilemez.
- Onay işlemi `SOZLESME` modülünde Onaylama yetkisi ister (yetkilendirme açıksa). Karar kayıtları ve onaylı doküman silinemez.
Bu modül maliyet, taahhüt ya da hakediş tutarına dokunmaz.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require

KINDS = {"MALZEME_ONAYI": "Malzeme onayı", "TEKNIK_BELGE": "Teknik belge", "SARTNAME": "Şartname", "SOZLESME_EKI": "Sözleşme eki",
         "DIGER": "Diğer"}
STATUSES = {"TASLAK": "Taslak", "INCELEMEDE": "İncelemede", "ONAYLI": "Onaylı", "REDDEDILDI": "Reddedildi", "REVIZYON": "Revizyon istendi"}
DECISIONS = {"ONAY": "Onay", "RED": "Ret", "REVIZYON": "Revizyon iste"}
LINKS = {"SOZLESME": ("pp_contracts", "Sözleşme", "title"), "SIPARIS": ("po_orders", "Sipariş", "order_no"),
         "TALEP": ("purchase_requests", "Satın alma talebi", "title"), "PAKET": ("sub_packages", "Alt yüklenici paketi", "name")}
_RESULT = {"ONAY": "ONAYLI", "RED": "REDDEDILDI", "REVIZYON": "REVIZYON"}


@dataclass(frozen=True)
class DocRow:
    id: int
    project_id: int
    kind: str
    title: str
    doc_no: str
    status: str
    link_label: str
    party: str
    version_no: int | None
    file_name: str
    last_decision: str


class DocumentService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path | str | None = None):
        self.conn = conn
        self.data_dir = Path(data_dir) if data_dir else None

    # --- doküman ---------------------------------------------------------------
    def _link_check(self, project_id: int, link_type: str | None, link_id: int | None) -> None:
        if link_type is None and link_id is None:
            return
        if link_type not in LINKS or link_id is None:
            raise ValidationError("Geçersiz bağlantı türü.")
        table = LINKS[link_type][0]
        row = self.conn.execute(f"SELECT project_id FROM {table} WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{LINKS[link_type][1]} bulunamadı.")
        if row["project_id"] is not None and row["project_id"] != project_id:
            raise ConflictError(f"{LINKS[link_type][1]} başka bir projeye ait; doküman yalnız aynı projedeki kayda bağlanabilir.")

    def create(self, project_id: int, kind: str, title: str, doc_no: str = "", link_type: str | None = None,
               link_id: int | None = None, party_id: int | None = None, created_by: str = "") -> int:
        if kind not in KINDS:
            raise ValidationError("Geçersiz doküman türü.")
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError("Proje bulunamadı.")
        if party_id is not None and self.conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
            raise NotFoundError("Firma bulunamadı.")
        self._link_check(project_id, link_type, link_id)
        with transaction(self.conn):
            did = self.conn.execute(
                "INSERT INTO doc_documents(project_id, kind, title, doc_no, link_type, link_id, party_id, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, kind, require(title, "Doküman adı"), clean(doc_no) or "", link_type, link_id, party_id,
                 clean(created_by) or "")).lastrowid
            audit.log(self.conn, "doc_document", did, "EKLE", new={"project": project_id, "kind": kind, "title": title})
        return did

    def _doc(self, doc_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM doc_documents WHERE id = ?", (doc_id,)).fetchone()
        if r is None:
            raise NotFoundError("Doküman bulunamadı.")
        return r

    def delete(self, doc_id: int) -> None:
        d = self._doc(doc_id)
        if self.conn.execute("SELECT 1 FROM doc_decisions WHERE document_id = ? AND decision = 'ONAY'", (doc_id,)).fetchone():
            raise ConflictError("Onaylanmış doküman silinemez.")
        with transaction(self.conn):
            audit.log(self.conn, "doc_document", doc_id, "SIL", old={"title": d["title"], "status": d["status"]})
            self.conn.execute("DELETE FROM doc_documents WHERE id = ?", (doc_id,))

    # --- sürümler ---------------------------------------------------------------
    def add_version(self, doc_id: int, path: Path | str | None = None, note: str = "", created_by: str = "") -> int:
        d = self._doc(doc_id)
        src = Path(path) if path else None
        if src is None and not clean(note):
            raise ValidationError("Sürüm için dosya ya da açıklama girin.")
        file_id, name = None, ""
        if src is not None:
            if not src.is_file():
                raise ValidationError(f"Dosya bulunamadı: {src}")
            if self.data_dir is None:
                raise ValidationError("Veri klasörü tanımlı değil; dosya eklenemez.")
            file_id, name = self._store(src), src.name
        with transaction(self.conn):
            no = self.conn.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM doc_versions WHERE document_id = ?", (doc_id,)).fetchone()[0]
            vid = self.conn.execute("INSERT INTO doc_versions(document_id, version_no, file_id, file_name, note, created_by)"
                                    " VALUES (?, ?, ?, ?, ?, ?)", (doc_id, no, file_id, name, clean(note) or "", clean(created_by) or "")).lastrowid
            if d["status"] != "TASLAK":
                self.conn.execute("UPDATE doc_documents SET status = 'TASLAK' WHERE id = ?", (doc_id,))
            audit.log(self.conn, "doc_version", vid, "EKLE", new={"document": doc_id, "version": no, "file": name})
        return vid

    def _store(self, src: Path) -> int:
        h = hashlib.sha256()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        row = self.conn.execute("SELECT id FROM source_files WHERE sha256 = ?", (sha,)).fetchone()
        if row:
            return row["id"]
        rel = f"KaynakDosyalar/Dokumanlar/{sha}{src.suffix.lower()}"
        dest = self.data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        return self.conn.execute("INSERT INTO source_files(original_name, sha256, stored_path, size_bytes) VALUES (?, ?, ?, ?)",
                                 (src.name, sha, rel, src.stat().st_size)).lastrowid

    def versions(self, doc_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT v.*, f.sha256, f.size_bytes, f.stored_path FROM doc_versions v LEFT JOIN source_files f ON f.id = v.file_id"
            " WHERE v.document_id = ? ORDER BY v.version_no DESC", (doc_id,)).fetchall()

    def file_path(self, version_id: int) -> Path | None:
        r = self.conn.execute("SELECT f.stored_path FROM doc_versions v JOIN source_files f ON f.id = v.file_id WHERE v.id = ?",
                              (version_id,)).fetchone()
        return (self.data_dir / r["stored_path"]) if r and self.data_dir else None

    def verify_file(self, version_id: int) -> tuple[bool, str]:
        """(sağlam mı, açıklama). Dosyasız sürüm sağlam sayılır."""
        r = self.conn.execute("SELECT f.sha256, f.stored_path FROM doc_versions v JOIN source_files f ON f.id = v.file_id WHERE v.id = ?",
                              (version_id,)).fetchone()
        if r is None:
            return True, "Dosyasız sürüm."
        p = self.data_dir / r["stored_path"] if self.data_dir else None
        if p is None or not p.is_file():
            return False, "Dosya bulunamadı."
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return (h.hexdigest() == r["sha256"]), ("Özet doğrulandı." if h.hexdigest() == r["sha256"] else "Dosya içeriği değişmiş.")

    # --- akış ---------------------------------------------------------------------
    def submit(self, doc_id: int, by: str = "") -> None:
        d = self._doc(doc_id)
        if d["status"] not in ("TASLAK", "REVIZYON", "REDDEDILDI"):
            raise ConflictError("Doküman zaten incelemede ya da onaylı.")
        if self.conn.execute("SELECT 1 FROM doc_versions WHERE document_id = ?", (doc_id,)).fetchone() is None:
            raise ValidationError("İncelemeye göndermeden önce en az bir sürüm ekleyin.")
        with transaction(self.conn):
            self.conn.execute("UPDATE doc_documents SET status = 'INCELEMEDE' WHERE id = ?", (doc_id,))
            audit.log(self.conn, "doc_document", doc_id, "GUNCELLE", old={"status": d["status"]}, new={"status": "INCELEMEDE"},
                      note=clean(by) or None)

    def decide(self, doc_id: int, decision: str, reason: str, decided_by: str) -> int:
        if decision not in DECISIONS:
            raise ValidationError("Geçersiz karar.")
        authz.require("SOZLESME", authz.APPROVE, "doküman kararı")
        d = self._doc(doc_id)
        if d["status"] != "INCELEMEDE":
            raise ConflictError("Karar yalnız incelemedeki dokümana verilir.")
        ver = self.conn.execute("SELECT id FROM doc_versions WHERE document_id = ? ORDER BY version_no DESC LIMIT 1", (doc_id,)).fetchone()
        ok, msg = self.verify_file(ver["id"])
        if decision == "ONAY" and not ok:
            raise ConflictError(f"Dosya doğrulanamadığı için onaylanamaz: {msg}")
        with transaction(self.conn):
            rid = self.conn.execute("INSERT INTO doc_decisions(document_id, version_id, decision, reason, decided_by) VALUES (?, ?, ?, ?, ?)",
                                    (doc_id, ver["id"], decision, require(reason, "Gerekçe"), require(decided_by, "Karar veren"))).lastrowid
            self.conn.execute("UPDATE doc_documents SET status = ? WHERE id = ?", (_RESULT[decision], doc_id))
            audit.log(self.conn, "doc_decision", rid, "EKLE", new={"document": doc_id, "decision": decision, "by": decided_by})
        return rid

    def decisions(self, doc_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT x.*, v.version_no FROM doc_decisions x JOIN doc_versions v ON v.id = x.version_id WHERE x.document_id = ? ORDER BY x.id DESC",
            (doc_id,)).fetchall()

    # --- listeleme ---------------------------------------------------------------
    def link_label(self, d: sqlite3.Row) -> str:
        if not d["link_type"]:
            return ""
        table, label, col = LINKS[d["link_type"]]
        r = self.conn.execute(f"SELECT {col} AS n FROM {table} WHERE id = ?", (d["link_id"],)).fetchone()
        return f"{label}: {r['n'] if r else '(silinmiş)'}"

    def documents(self, project_id: int, kind: str | None = None, status: str | None = None, party_id: int | None = None) -> list[DocRow]:
        sql = "SELECT * FROM doc_documents WHERE project_id = ?"
        args: list = [project_id]
        for col, val in (("kind", kind), ("status", status), ("party_id", party_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        out = []
        for d in self.conn.execute(sql + " ORDER BY id DESC", args).fetchall():
            v = self.conn.execute("SELECT version_no, file_name FROM doc_versions WHERE document_id = ? ORDER BY version_no DESC LIMIT 1",
                                  (d["id"],)).fetchone()
            last = self.conn.execute("SELECT decision FROM doc_decisions WHERE document_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
            party = self.conn.execute("SELECT name FROM parties WHERE id = ?", (d["party_id"],)).fetchone() if d["party_id"] else None
            out.append(DocRow(d["id"], d["project_id"], d["kind"], d["title"], d["doc_no"], d["status"], self.link_label(d),
                              party["name"] if party else "", v["version_no"] if v else None, v["file_name"] if v else "",
                              DECISIONS[last["decision"]] if last else ""))
        return out

    def pending_count(self, project_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM doc_documents WHERE project_id = ? AND status = 'INCELEMEDE'", (project_id,)).fetchone()[0]
