"""İhale hattı ve alınan işler: ihale aşamaları, ihalenin kazanılması ve alınan iş aşamaları.

Aynı proje kaydı iki çalışma alanında görünür: "Girilecek İhaleler" (tender_status dolu olanlar; kazanılan ve
kaybedilen ihaleler de geçmiş olarak kalır) ve "Alınan İşler" (work_stage dolu olanlar). İhale kazanıldığında
kayıt kopyalanmaz; alınan işe aktarılır, ihale bilgileri ve aşama geçmişi korunur.
Eski proje durumu (status) aşamalarla uyumlu tutulur: ihale → TASLAK, kayıp/iptal → ARSIV, iş aşamaları →
DEVAM / TAMAMLANDI / ARSIV.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, validate_iso_date

TENDER_STATUSES = {"TAKIP": "Takipte", "HAZIRLIK": "Teklif hazırlanıyor", "TEKLIF_VERILDI": "Teklif verildi",
                   "SONUC_BEKLENIYOR": "Sonuç bekleniyor", "KAZANILDI": "Kazanıldı", "KAZANILAMADI": "Kazanılamadı",
                   "IPTAL": "İhale iptal"}
WORK_STAGES = {"KAZANILMIS": "Kazanıldı (sözleşme bekliyor)", "SOZLESME": "Sözleşme sürecinde", "DEVAM": "Devam ediyor",
               "TAMAMLANDI": "Tamamlandı", "KAPANDI": "Kapandı / arşiv"}
_STATUS_OF_WORK = {"KAZANILMIS": "DEVAM", "SOZLESME": "DEVAM", "DEVAM": "DEVAM", "TAMAMLANDI": "TAMAMLANDI",
                   "KAPANDI": "ARSIV"}
_LOST = ("KAZANILAMADI", "IPTAL")


def sync_from_status(conn: sqlite3.Connection, pid: int) -> None:
    """Proje formundan (status) yapılan değişiklikten sonra aşama sütunlarını uyumlu tutar.
    Yazma transaction'ı içinde çağrılır. Aşama sütunları henüz yoksa (eski şema) bir şey yapmaz."""
    if not any(c[1] == "tender_status" for c in conn.execute("PRAGMA table_info(projects)")):
        return
    r = conn.execute("SELECT status, tender_status, work_stage FROM projects WHERE id = ?", (pid,)).fetchone()
    status, tender, work = r["status"], r["tender_status"], r["work_stage"]
    if status == "TASLAK":
        if work is None and tender is None:
            conn.execute("UPDATE projects SET tender_status = 'HAZIRLIK' WHERE id = ?", (pid,))
        return
    new_work = {"DEVAM": work if work in ("KAZANILMIS", "SOZLESME", "DEVAM") else "DEVAM",
                "TAMAMLANDI": "TAMAMLANDI", "ARSIV": "KAPANDI"}[status]
    if work is None and status == "ARSIV" and tender is not None and tender != "KAZANILDI":
        if tender not in _LOST:
            conn.execute("UPDATE projects SET tender_status = 'IPTAL' WHERE id = ?", (pid,))
            conn.execute("INSERT INTO project_stage_history(project_id, kind, stage) VALUES (?, 'IHALE', 'IPTAL')",
                         (pid,))
        return
    if new_work != work:
        conn.execute("UPDATE projects SET work_stage = ?, tender_status = CASE WHEN tender_status IS NOT NULL AND"
                     " tender_status NOT IN ('KAZANILAMADI', 'IPTAL') THEN 'KAZANILDI' ELSE tender_status END"
                     " WHERE id = ?", (new_work, pid))
        conn.execute("INSERT INTO project_stage_history(project_id, kind, stage) VALUES (?, 'IS', ?)", (pid, new_work))


@dataclass(frozen=True)
class PipelineRow:
    id: int
    name: str
    authority: str | None
    ikn: str | None
    tender_date: str | None
    tender_status: str | None
    work_stage: str | None
    awarded_date: str | None
    note: str | None

    @property
    def tender_label(self) -> str:
        return TENDER_STATUSES.get(self.tender_status or "", "")

    @property
    def stage_label(self) -> str:
        return WORK_STAGES.get(self.work_stage or "", "")


class PipelineService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _rows(self, where: str, order: str) -> list[PipelineRow]:
        rows = self.conn.execute(
            "SELECT p.id, p.name, a.name AS authority, p.ikn, p.tender_date, p.tender_status, p.work_stage,"
            " p.awarded_date, p.stage_note FROM projects p LEFT JOIN authorities a ON a.id = p.authority_id"
            f" WHERE {where} ORDER BY {order}").fetchall()
        return [PipelineRow(*tuple(r)) for r in rows]

    def tenders(self) -> list[PipelineRow]:
        """Girilecek ihaleler (kazanılan ve kaybedilenler geçmiş olarak dahil); tarihi yakın olan üstte."""
        return self._rows("p.tender_status IS NOT NULL", "COALESCE(p.tender_date, '9999'), p.id")

    def works(self) -> list[PipelineRow]:
        """Alınan işler."""
        return self._rows("p.work_stage IS NOT NULL", "p.id DESC")

    def _project(self, pid: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if r is None:
            raise NotFoundError("Proje bulunamadı.")
        return r

    def history(self, pid: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM project_stage_history WHERE project_id = ? ORDER BY id",
                                 (pid,)).fetchall()

    def _log(self, pid: int, kind: str, stage: str, note: str | None) -> None:
        self.conn.execute("INSERT INTO project_stage_history(project_id, kind, stage, note) VALUES (?, ?, ?, ?)",
                          (pid, kind, stage, note))

    def set_tender_status(self, pid: int, status: str, note: str | None = None) -> None:
        if status not in TENDER_STATUSES:
            raise ValidationError("Geçersiz ihale aşaması.")
        if status == "KAZANILDI":
            raise ValidationError("İhalenin kazanıldığını 'Kazanıldı → Alınan İşe Aktar' ile kaydedin.")
        p = self._project(pid)
        if p["work_stage"] is not None:
            raise ConflictError("Bu ihale alınan işe aktarılmış; ihale aşaması değiştirilemez.")
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET tender_status = ?, stage_note = COALESCE(?, stage_note),"
                              " status = ? WHERE id = ?",
                              (status, clean(note), "ARSIV" if status in _LOST else "TASLAK", pid))
            self._log(pid, "IHALE", status, clean(note))
            audit.log(self.conn, "project", pid, "GUNCELLE", old={"tender_status": p["tender_status"]},
                      new={"tender_status": status}, note=clean(note))

    def award(self, pid: int, awarded_date: str, note: str | None = None) -> None:
        """İhale kazanıldı: kayıt alınan işe (sözleşme bekliyor) aktarılır; ihale bilgileri korunur."""
        authz.require("PROJE", authz.APPROVE, "ihale kazanıldı kaydı")
        p = self._project(pid)
        if p["tender_status"] is None:
            raise ValidationError("Bu kayıt bir ihale değil.")
        if p["work_stage"] is not None:
            raise ConflictError("Bu ihale zaten alınan işe aktarılmış.")
        if p["tender_status"] in _LOST:
            raise ConflictError("Kazanılamayan ya da iptal edilen ihale alınan işe aktarılamaz; önce aşamasını "
                                "düzeltin.")
        d = validate_iso_date(awarded_date, "Kazanma tarihi", required=True)
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET tender_status = 'KAZANILDI', work_stage = 'KAZANILMIS',"
                              " awarded_date = ?, status = 'DEVAM', stage_note = COALESCE(?, stage_note) WHERE id = ?",
                              (d, clean(note), pid))
            self._log(pid, "IHALE", "KAZANILDI", clean(note))
            self._log(pid, "IS", "KAZANILMIS", None)
            audit.log(self.conn, "project", pid, "GUNCELLE", new={"tender_status": "KAZANILDI",
                                                                   "work_stage": "KAZANILMIS"}, note="İhale kazanıldı")

    def set_work_stage(self, pid: int, stage: str, note: str | None = None) -> None:
        if stage not in WORK_STAGES:
            raise ValidationError("Geçersiz iş aşaması.")
        p = self._project(pid)
        if p["work_stage"] is None:
            raise ConflictError("Bu kayıt henüz alınan iş değil; önce ihale kazanıldı olarak aktarın.")
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET work_stage = ?, status = ?, stage_note = COALESCE(?, stage_note)"
                              " WHERE id = ?", (stage, _STATUS_OF_WORK[stage], clean(note), pid))
            self._log(pid, "IS", stage, clean(note))
            audit.log(self.conn, "project", pid, "GUNCELLE", old={"work_stage": p["work_stage"]},
                      new={"work_stage": stage}, note=clean(note))

    def start_tender(self, pid: int, status: str = "TAKIP") -> None:
        """İhalesiz kaydı (ör. doğrudan açılmış proje) ihale takibine alır."""
        p = self._project(pid)
        if p["work_stage"] is not None:
            raise ConflictError("Alınan iş ihale takibine alınamaz.")
        if status not in TENDER_STATUSES or status == "KAZANILDI":
            raise ValidationError("Geçersiz ihale aşaması.")
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET tender_status = ?, status = 'TASLAK' WHERE id = ?", (status, pid))
            self._log(pid, "IHALE", status, None)

    def start_work(self, pid: int, stage: str = "DEVAM", direct: bool = False) -> None:
        """İhalesiz (doğrudan alınan) projeyi alınan işlere ekler. `direct`: yeni açılmış, ihale geçmişi olmayan
        kaydın ihale aşaması temizlenir."""
        p = self._project(pid)
        if p["tender_status"] not in (None, "KAZANILDI") and p["work_stage"] is None and not direct:
            raise ConflictError("İhale sürecindeki kayıt için 'Kazanıldı → Alınan İşe Aktar' kullanın.")
        if direct and len(self.history(pid)) > 0:
            raise ConflictError("İhale geçmişi olan kayıt doğrudan iş yapılamaz.")
        if stage not in WORK_STAGES:
            raise ValidationError("Geçersiz iş aşaması.")
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET work_stage = ?, status = ?, tender_status = CASE WHEN ? THEN NULL"
                              " ELSE tender_status END WHERE id = ?", (stage, _STATUS_OF_WORK[stage], int(direct), pid))
            self._log(pid, "IS", stage, None)
