"""Projeler ve keşif listeleri (başlık düzeyi) ile keşif kalemleri."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, estimate_items, projects
from yapimmaliyet.db.repositories.estimate_items import EstimateItem
from yapimmaliyet.db.repositories.projects import Estimate, Project
from yapimmaliyet.domain.tender import DISCIPLINES, PRICING_METHODS, TENDER_MODELS, join_disciplines
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.pipeline_service import sync_from_status
from yapimmaliyet.services.validators import clean, require, validate_iso_date, validate_price, validate_quantity

OFFER_TYPES = {"BIRIM_FIYAT": "Birim fiyat teklif", "GOTURU_BEDEL": "Götürü bedel teklif"}
PROJECT_STATUSES = {"TASLAK": "Taslak", "DEVAM": "Devam ediyor", "TAMAMLANDI": "Tamamlandı", "ARSIV": "Arşiv"}


class ProjectService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- projeler ----------------------------------------------------------
    def list_projects(self, include_archived: bool = True) -> list[Project]:
        return projects.list_projects(self.conn, include_archived)

    def get_project(self, project_id: int) -> Project:
        p = projects.get_project(self.conn, project_id)
        if p is None:
            raise NotFoundError("Proje bulunamadı.")
        return p

    def _fields(self, name, authority_id, ikn, tender_date, work_type, offer_type, status, notes,
                tender_model, disciplines) -> dict:
        if offer_type is not None and offer_type not in OFFER_TYPES:
            raise ValidationError("Geçersiz teklif türü.")
        model = tender_model or ("ANAHTAR_TESLIM" if offer_type == "GOTURU_BEDEL" else "BIRIM_FIYAT")
        if model not in TENDER_MODELS:
            raise ValidationError("Geçersiz ihale modeli.")
        derived = "GOTURU_BEDEL" if model == "ANAHTAR_TESLIM" else "BIRIM_FIYAT"   # eski sütunla uyum
        if offer_type is not None and tender_model is not None and offer_type != derived:
            raise ValidationError("Teklif türü ile ihale modeli uyuşmuyor.")
        if status not in PROJECT_STATUSES:
            raise ValidationError("Geçersiz proje durumu.")
        try:
            disc = join_disciplines(disciplines or ())
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return dict(name=require(name, "Proje adı"), authority_id=authority_id, ikn=clean(ikn),
                    tender_date=validate_iso_date(tender_date, "İhale tarihi"), work_type=clean(work_type),
                    offer_type=derived, status=status, notes=clean(notes), tender_model=model, disciplines=disc)

    def create_project(self, name: str, authority_id: int | None = None, ikn: str | None = None,
                       tender_date: str | None = None, work_type: str | None = None,
                       offer_type: str | None = None, status: str = "TASLAK", notes: str | None = None,
                       tender_model: str | None = None, disciplines=()) -> int:
        f = self._fields(name, authority_id, ikn, tender_date, work_type, offer_type, status, notes,
                         tender_model, disciplines)
        with transaction(self.conn):
            pid = projects.insert_project(self.conn, **f)
            sync_from_status(self.conn, pid)
            audit.log(self.conn, "project", pid, "EKLE", new=f)
        return pid

    def update_project(self, project_id: int, name: str, authority_id: int | None, ikn: str | None,
                       tender_date: str | None, work_type: str | None, offer_type: str | None, status: str,
                       notes: str | None, tender_model: str | None = None, disciplines=None) -> None:
        """`tender_model`/`disciplines` verilmezse mevcut değerler korunur (eski çağrılarla uyum)."""
        old = self.get_project(project_id)
        if tender_model is None and offer_type is not None:
            same = (old.offer_type == offer_type)
            tender_model = old.tender_model if same else None
        f = self._fields(name, authority_id, ikn, tender_date, work_type, offer_type, status, notes,
                         tender_model, old.disciplines.split(",") if disciplines is None and old.disciplines
                         else disciplines)
        with transaction(self.conn):
            projects.update_project(self.conn, project_id, **f)
            if f["status"] != old.status:
                sync_from_status(self.conn, project_id)
            audit.log(self.conn, "project", project_id, "GUNCELLE", old={"name": old.name, "status": old.status}, new=f)

    def delete_project(self, project_id: int, with_estimates: bool = False) -> None:
        """Projeyi siler. Keşif listeleri varsa `with_estimates=True` ile (kullanıcı onayıyla) keşifler de silinir:
        keşif kalemleri, bunlara bağlı hesaplar (kesinleşmiş olanlar dahil), ihale teklifleri ve maliyet çalışmaları
        keşifle birlikte gider. Firma kayıtlarına, satın alma taleplerine ve tekliflerine dokunulmaz: bunlar birden
        fazla projede geçerli olabilir, yalnız proje bağlantıları kalkar."""
        p = self.get_project(project_id)
        if p.estimate_count and not with_estimates:
            raise ConflictError(f"Projeye bağlı {p.estimate_count} keşif listesi var.")
        final = [e.name for e in projects.list_estimates(self.conn, project_id)
                 if projects.estimate_has_final_calc(self.conn, e.id)]
        if final:   # kesinleşmiş hesap anlık görüntüsü resmî kayıttır; veritabanı değiştirilmesine/silinmesine izin vermez
            raise ConflictError("Kesinleşmiş yaklaşık maliyet kaydı olan keşif var (" + ", ".join(final) + "). Kesinleşmiş "
                                "hesaplar değiştirilemez resmî kayıttır, silinemez; projeyi silmek yerine arşivleyin.")
        with transaction(self.conn):
            for e in projects.list_estimates(self.conn, project_id):
                self.conn.execute("DELETE FROM calculations WHERE estimate_id = ?", (e.id,))
                projects.delete_estimate(self.conn, e.id)
                audit.log(self.conn, "estimate", e.id, "SIL", old={"name": e.name, "project": p.name},
                          note="Proje silinirken")
            self.conn.execute("UPDATE new_price_records SET project_id = NULL WHERE project_id = ?", (project_id,))
            projects.delete_project(self.conn, project_id)
            audit.log(self.conn, "project", project_id, "SIL", old={"name": p.name, "estimates": p.estimate_count})

    # --- keşif listeleri ---------------------------------------------------
    # --- etkin proje (ana penceredeki ortak proje bağlamı; veritabanıyla birlikte saklanır) ---------------
    ACTIVE_KEY = "active_project_id"

    def active_project_id(self) -> int | None:
        """Kayıtlı etkin proje; proje silinmişse None."""
        r = self.conn.execute("SELECT value FROM app_meta WHERE key = ?", (self.ACTIVE_KEY,)).fetchone()
        if r is None or not r[0].isdigit():
            return None
        pid = int(r[0])
        return pid if projects.get_project(self.conn, pid) is not None else None

    def set_active_project(self, project_id: int | None) -> None:
        if project_id is not None:
            self.get_project(project_id)
        with transaction(self.conn):
            if project_id is None:
                self.conn.execute("DELETE FROM app_meta WHERE key = ?", (self.ACTIVE_KEY,))
            else:
                self.conn.execute("INSERT INTO app_meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                                  "SET value = excluded.value", (self.ACTIVE_KEY, str(project_id)))

    def list_estimates(self, project_id: int) -> list[Estimate]:
        return projects.list_estimates(self.conn, project_id)

    def get_estimate(self, estimate_id: int) -> Estimate:
        e = projects.get_estimate(self.conn, estimate_id)
        if e is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        return e

    def create_estimate(self, project_id: int, name: str) -> int:
        self.get_project(project_id)
        name = require(name, "Keşif adı")
        with transaction(self.conn):
            eid = projects.insert_estimate(self.conn, project_id, name)
            audit.log(self.conn, "estimate", eid, "EKLE", new={"name": name})
        return eid

    def delete_estimate(self, estimate_id: int) -> None:
        e = self.get_estimate(estimate_id)
        if projects.estimate_has_final_calc(self.conn, estimate_id):
            raise ConflictError("Bu keşfe ait kesinleştirilmiş hesap var; keşif silinemez (geçmiş korunur).")
        with transaction(self.conn):
            projects.delete_estimate(self.conn, estimate_id)
            audit.log(self.conn, "estimate", estimate_id, "SIL", old={"name": e.name, "revision": e.revision,
                                                                      "items": e.item_count})

    def set_source_priority(self, estimate_id: int, book_ids: list[int]) -> None:
        self.get_estimate(estimate_id)
        if len(set(book_ids)) != len(book_ids):
            raise ValidationError("Kaynak önceliğinde aynı kitap birden fazla kez seçilemez.")
        with transaction(self.conn):
            projects.set_priority(self.conn, estimate_id, book_ids)

    def set_index_settings(self, estimate_id: int, ym_date: str | None, series_id: int | None) -> None:
        """Endeks güncellemesi için yaklaşık maliyet hesap tarihi ve varsayılan endeks serisi (boş: Yİ-ÜFE)."""
        old = self.get_estimate(estimate_id)
        self._editable(estimate_id)
        ym_date = validate_iso_date(ym_date, "Yaklaşık maliyet hesap tarihi")
        self._check_series(series_id)
        with transaction(self.conn):
            projects.set_index_settings(self.conn, estimate_id, ym_date, series_id)
            audit.log(self.conn, "estimate", estimate_id, "GUNCELLE",
                      old={"ym_date": old.ym_date, "index_series_id": old.index_series_id},
                      new={"ym_date": ym_date, "index_series_id": series_id})

    def set_items_index_series(self, item_ids: list[int], series_id: int | None) -> None:
        """Seçili kalemlerin endeks sınıfı (None: keşifteki varsayılan seri kullanılır)."""
        if not item_ids:
            raise ValidationError("Kalem seçilmedi.")
        estimates = {self.get_item(i).estimate_id for i in item_ids}
        for eid in estimates:
            self._editable(eid)
        self._check_series(series_id)
        with transaction(self.conn):
            estimate_items.set_index_series(self.conn, item_ids, series_id)
            audit.log(self.conn, "estimate_item", None, "TOPLU", new={"items": item_ids, "index_series_id": series_id},
                      note="Endeks sınıfı atandı")

    def _check_series(self, series_id: int | None) -> None:
        if series_id is not None and self.conn.execute("SELECT 1 FROM index_series WHERE id = ?",
                                                       (series_id,)).fetchone() is None:
            raise NotFoundError("Endeks serisi bulunamadı.")

    def set_locked(self, estimate_id: int, locked: bool) -> None:
        self.get_estimate(estimate_id)
        with transaction(self.conn):
            projects.set_locked(self.conn, estimate_id, locked)
            audit.log(self.conn, "estimate", estimate_id, "GUNCELLE", new={"locked": locked})

    # --- kalemler ----------------------------------------------------------
    def items(self, estimate_id: int) -> list[EstimateItem]:
        return estimate_items.list_items(self.conn, estimate_id)

    def get_item(self, item_id: int) -> EstimateItem:
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        return it

    def _editable(self, estimate_id: int) -> None:
        if self.get_estimate(estimate_id).locked:
            raise ConflictError("Keşif kilitli; değişiklik için önce kilidi kaldırın.")

    def add_item(self, estimate_id: int, code: str | None, description: str | None, unit: str | None,
                 quantity, work_group: str | None = None, notes: str | None = None,
                 discipline: str | None = None, pricing_method: str | None = None) -> int:
        self._editable(estimate_id)
        self._check_class(discipline, pricing_method)
        code, description = clean(code), clean(description)
        if not code and not description:
            raise ValidationError("Poz no ya da imalat açıklamasından en az biri girilmelidir.")
        q = validate_quantity(quantity)
        with transaction(self.conn):
            line = estimate_items.next_line_no(self.conn, estimate_id)
            iid = estimate_items.insert_item(self.conn, estimate_id, line, clean(work_group), code, description,
                                             clean(unit), q, clean(notes), discipline, pricing_method)
            audit.log(self.conn, "estimate_item", iid, "EKLE", new={"code": code, "quantity": q})
        return iid

    def duplicate_item(self, item_id: int) -> int:
        """Kalemin kopyasını keşfin sonuna ekler (poz no, açıklama, birim, miktar, grup, not, sınıf); eşleşme kopyalanmaz."""
        it = self.get_item(item_id)
        return self.add_item(it.estimate_id, it.code, it.description, it.unit, it.quantity, it.work_group, it.notes,
                             it.discipline, it.pricing_method)

    def update_item(self, item_id: int, code: str | None, description: str | None, unit: str | None,
                    quantity, work_group: str | None, notes: str | None) -> None:
        old = self.get_item(item_id)
        self._editable(old.estimate_id)
        code, description = clean(code), clean(description)
        if not code and not description:
            raise ValidationError("Poz no ya da imalat açıklamasından en az biri girilmelidir.")
        q = validate_quantity(quantity)
        with transaction(self.conn):
            estimate_items.update_item(self.conn, item_id, clean(work_group), code, description, clean(unit), q,
                                       clean(notes))
            audit.log(self.conn, "estimate_item", item_id, "GUNCELLE",
                      old={"code": old.code, "unit": old.unit, "quantity": old.quantity},
                      new={"code": code, "unit": unit, "quantity": q})

    def delete_item(self, item_id: int) -> None:
        old = self.get_item(item_id)
        self._editable(old.estimate_id)
        with transaction(self.conn):
            estimate_items.delete_item(self.conn, item_id)
            audit.log(self.conn, "estimate_item", item_id, "SIL", old={"code": old.code, "quantity": old.quantity})

    @staticmethod
    def _check_class(discipline: str | None, pricing_method: str | None) -> None:
        if discipline is not None and discipline not in DISCIPLINES:
            raise ValidationError("Geçersiz disiplin.")
        if pricing_method is not None and pricing_method not in PRICING_METHODS:
            raise ValidationError("Geçersiz teklif yöntemi.")

    def set_items_discipline(self, item_ids: list[int], discipline: str | None) -> None:
        self._classify(item_ids, "discipline", discipline)

    def set_items_pricing_method(self, item_ids: list[int], method: str | None) -> None:
        """Karma modelde kalemin birim fiyatlı mı götürü bedelli mi olduğunu belirler (None: atanmamış)."""
        self._classify(item_ids, "pricing_method", method)

    def _classify(self, item_ids: list[int], column: str, value: str | None) -> None:
        if not item_ids:
            return
        self._check_class(value if column == "discipline" else None, value if column == "pricing_method" else None)
        for eid in {self.get_item(i).estimate_id for i in item_ids}:
            self._editable(eid)
        with transaction(self.conn):
            estimate_items.set_classification(self.conn, item_ids, column, value)
            audit.log(self.conn, "estimate_item", item_ids[0], "GUNCELLE", new={column: value, "items": item_ids})

    def set_price_override(self, item_id: int, price: Decimal | None) -> None:
        """Kullanıcının birim fiyatı elle değiştirmesi (None: değişikliği geri al). Raporlarda işaretlenir."""
        old = self.get_item(item_id)
        self._editable(old.estimate_id)
        p = None if price is None else validate_price(price)
        with transaction(self.conn):
            estimate_items.set_override(self.conn, item_id, p)
            audit.log(self.conn, "estimate_item", item_id, "GUNCELLE",
                      old={"user_price_override": old.user_price_override}, new={"user_price_override": p})
