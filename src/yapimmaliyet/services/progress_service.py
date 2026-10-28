"""Hakediş sözleşmeleri: sözleşme bilgileri, sözleşme kalemleri (fiyat değişikliği izli) ve kesinti türleri.

Taşeron hakedişi (ana yüklenici → taşeron) ve idari hakediş (yüklenici → idare) aynı yapıyı kullanır; `kind` alanı
ayırır. Kesinti oranları kodda sabit değildir: sözleşme bazında kullanıcı tanımlar. `PRESETS` yalnızca kullanıcının
örnek hakedişlerinden alınmış başlangıç önerileridir; eklenince düzenlenebilir ve her biri bu notu taşır.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, parties, projects, tender_bids
from yapimmaliyet.db.repositories import progress as repo
from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.domain.progress import DEDUCTION_BASES, ITEM_SOURCES, KINDS, VAT_BASES
from yapimmaliyet.domain.tender import DISCIPLINES, TENDER_MODELS
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require, validate_iso_date

PRESET_NOTE = ("Kullanıcının örnek hakedişinden ({src}) alınmış öneri; oran ve tabanı sözleşmeye ve yürürlükteki "
               "mevzuata göre doğrulayın.")
# (ad, taban, oran, avans mı, teminat mı, örnek kaynak)
PRESETS = {
    "TASERON": [
        ("Nakit teminat kesintisi", "BU_HAKEDIS", "0.05", False, True, "Gasway/Refleks: hakediş tutarı × %5"),
        ("KDV tevkifatı", "KDV", "0.4", False, False, "Refleks: KDV × 4/10"),
        ("SGK kesintisi", "MANUEL", None, False, False, "4-Hak.Takip Tab."),
        ("Malzeme bedeli kesintisi", "MANUEL", None, False, False, "Malzeme ve İSG kesinti sayfası"),
        ("Yemek kesintisi", "MANUEL", None, False, False, "4-Hak.Takip Tab."),
        ("Avans mahsubu", "MANUEL", None, True, False, "4-Hak.Takip Tab."),
        ("Diğer kesintiler", "MANUEL", None, False, False, "4-Hak.Takip Tab."),
    ],
    "IDARI": [
        ("Gelir / kurumlar vergisi", "BU_HAKEDIS_EKSI_AVANS", "0.05", False, False, "Silopi 13: (E−g) × %5"),
        ("Damga vergisi", "BU_HAKEDIS_EKSI_AVANS", "0.00948", False, False, "Silopi 13: (E−g) × binde 9,48"),
        ("KDV tevkifatı", "KDV", "0.4", False, False, "Silopi 13: F × 4/10"),
        ("Sosyal Sigortalar Kurumu kesintisi", "MANUEL", None, False, False, "Silopi 13 H.Rapor d)"),
        ("Teknik personel cezası", "MANUEL", None, False, False, "Silopi 13 H.Rapor e)"),
        ("Gecikme cezası", "MANUEL", None, False, False, "Silopi 13 H.Rapor f)"),
        ("Avans mahsubu", "MANUEL", None, True, False, "Silopi 13 H.Rapor g)"),
        ("Diğer ceza ve kesintiler", "MANUEL", None, False, False, "Silopi 13 H.Rapor j)"),
    ],
}


def _dec(value, label: str, *, allow_none: bool = False, min_zero: bool = True) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        raise ValidationError(f"{label} boş bırakılamaz.")
    try:
        d = to_decimal(str(value).replace(",", ".") if isinstance(value, str) else value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçerli bir sayı değil: {value}") from exc
    if min_zero and d < 0:
        raise ValidationError(f"{label} negatif olamaz.")
    return d


class ProgressService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- sözleşmeler -----------------------------------------------------------
    def contracts(self, kind: str) -> list[repo.Contract]:
        return repo.list_contracts(self.conn, kind)

    def contract(self, contract_id: int) -> repo.Contract:
        c = repo.get_contract(self.conn, contract_id)
        if c is None:
            raise NotFoundError("Hakediş sözleşmesi bulunamadı.")
        return c

    def _contract_data(self, data: dict) -> dict:
        d = dict(data)
        if d.get("kind") not in KINDS:
            raise ValidationError("Geçersiz hakediş türü.")
        d["title"] = require(d.get("title"), "İşin adı")
        d["employer"] = require(d.get("employer"), "İşveren / idare")
        d["contractor"] = require(d.get("contractor"), "Yüklenici / taşeron")
        if d.get("model") not in TENDER_MODELS:
            raise ValidationError("Sözleşme modeli seçilmelidir.")
        d["contract_amount"] = _dec(d.get("contract_amount"), "Sözleşme bedeli")
        d["vat_rate"] = _dec(d.get("vat_rate", "0.20"), "KDV oranı")
        if d["vat_rate"] >= 1:
            raise ValidationError("KDV oranı kesir olarak girilmelidir (ör. 0,20).")
        d["advance_total"] = _dec(d.get("advance_total") or "0", "Verilen avans")
        if d.get("vat_base", "BRUT") not in VAT_BASES:
            raise ValidationError("Geçersiz KDV tabanı.")
        d.setdefault("vat_base", "BRUT")
        d["currency"] = clean(d.get("currency")) or "TL"
        for f, label in (("contract_date", "Sözleşme tarihi"), ("start_date", "Başlangıç tarihi"),
                         ("end_date", "Bitiş tarihi")):
            d[f] = validate_iso_date(d.get(f), label)
        if d["start_date"] and d["end_date"] and d["end_date"] < d["start_date"]:
            raise ValidationError("Bitiş tarihi başlangıç tarihinden önce olamaz.")
        if d.get("project_id") is not None and projects.get_project(self.conn, d["project_id"]) is None:
            raise ValidationError("Bağlanan proje bulunamadı.")
        for f in ("contract_no", "ikn", "notes"):
            d[f] = clean(d.get(f))
        sigs = d.get("signatures") or []
        d["signatures"] = [{"role": clean(s.get("role")) or "", "name": clean(s.get("name")) or "",
                            "title": clean(s.get("title")) or ""} for s in sigs if any(s.values())]
        return d

    def create_contract(self, data: dict) -> int:
        d = self._contract_data(data)
        with transaction(self.conn):
            cid = repo.insert_contract(self.conn, d)
            parties.link_unlinked(self.conn)
            audit.log(self.conn, "pp_contract", cid, "EKLE", new={"title": d["title"], "kind": d["kind"]})
        return cid

    def update_contract(self, contract_id: int, data: dict) -> None:
        old = self.contract(contract_id)
        d = self._contract_data({**data, "kind": old.kind})
        approved = any(p.status in ("ONAY", "ODENDI") for p in repo.list_payments(self.conn, contract_id))
        if approved and (d["model"] != old.model or d["vat_base"] != old.vat_base):
            raise ConflictError("Onaylı hakedişi olan sözleşmenin modeli ve KDV tabanı değiştirilemez.")
        with transaction(self.conn):
            repo.update_contract(self.conn, contract_id, d)
            parties.link_unlinked(self.conn)
            audit.log(self.conn, "pp_contract", contract_id, "GUNCELLE",
                      old={"amount": old.contract_amount, "vat_rate": old.vat_rate},
                      new={"amount": d["contract_amount"], "vat_rate": d["vat_rate"]})

    def delete_contract(self, contract_id: int) -> None:
        c = self.contract(contract_id)
        pays = repo.list_payments(self.conn, contract_id)
        if any(p.status not in ("TASLAK", "KONTROL") for p in pays):
            raise ConflictError("Onaylanmış hakedişi olan sözleşme silinemez.")
        with transaction(self.conn):
            for p in pays:
                repo.delete_payment(self.conn, p.id)
            repo.delete_contract(self.conn, contract_id)
            audit.log(self.conn, "pp_contract", contract_id, "SIL", old={"title": c.title})

    # --- kalemler ----------------------------------------------------------------
    def items(self, contract_id: int):
        return repo.list_items(self.conn, contract_id)

    def _item_data(self, contract_id: int, data: dict) -> dict:
        c = self.contract(contract_id)
        d = dict(data)
        d["description"] = require(d.get("description"), "İmalatın tanımı")
        for f in ("code", "unit", "group_name", "source_ref"):
            d[f] = clean(d.get(f))
        if d.get("discipline") not in (None, "") and d["discipline"] not in DISCIPLINES:
            raise ValidationError("Geçersiz disiplin.")
        d["discipline"] = d.get("discipline") or None
        method = d.get("method") or {"BIRIM_FIYAT": "BIRIM_FIYAT", "ANAHTAR_TESLIM": "GOTURU"}.get(c.model)
        if method not in ("BIRIM_FIYAT", "GOTURU"):
            raise ValidationError("Karma sözleşmede kalemin yöntemi (birim fiyat / götürü) seçilmelidir.")
        if c.model == "BIRIM_FIYAT" and method == "GOTURU" or c.model == "ANAHTAR_TESLIM" and method == "BIRIM_FIYAT":
            raise ValidationError(f"{TENDER_MODELS[c.model]} sözleşmede bu yöntem kullanılamaz; model karma olmalı.")
        d["method"] = method
        if d.get("source", "SOZLESME") not in ITEM_SOURCES:
            raise ValidationError("Geçersiz kalem kaynağı.")
        d["source"] = d.get("source") or "SOZLESME"
        if d["source"] != "SOZLESME" and not d["source_ref"]:
            raise ValidationError("Değişiklik / yeni birim fiyat / ilave iş kalemi için onay tarihi ve sayısı girilmelidir.")
        if method == "GOTURU":
            d["lump_amount"] = _dec(d.get("lump_amount"), "Götürü kalem bedeli")
            d["unit_price"] = d["contract_qty"] = None
            d["unit"] = d["unit"] or "%"
        else:
            d["unit_price"] = _dec(d.get("unit_price"), "Birim fiyat", allow_none=True)
            d["contract_qty"] = _dec(d.get("contract_qty"), "Sözleşme miktarı", allow_none=True)
            d["lump_amount"] = None
            if not d["unit"]:
                raise ValidationError("Birim fiyatlı kalemin birimi girilmelidir.")
        return d

    def add_item(self, contract_id: int, data: dict) -> int:
        d = self._item_data(contract_id, data)
        d["line_no"] = d.get("line_no") or repo.next_line_no(self.conn, contract_id)
        with transaction(self.conn):
            iid = repo.insert_item(self.conn, contract_id, d)
            audit.log(self.conn, "pp_item", iid, "EKLE", new={"code": d["code"], "source": d["source"]})
        return iid

    def update_item(self, item_id: int, data: dict, reason: str | None = None, ref: str | None = None) -> None:
        """Onaylı hakedişte kullanılmış kalemde fiyat/miktar/bedel değişikliği gerekçe ister ve izlenir;
        onaylı hakedişlerin kayıtları (kesinleşmiş içerik) bundan etkilenmez."""
        found = repo.get_item(self.conn, item_id)
        if found is None:
            raise NotFoundError("Kalem bulunamadı.")
        contract_id, old = found
        d = self._item_data(contract_id, {**data, "line_no": old.line_no})
        changes = [(f, getattr(old, f), d[f]) for f in ("unit_price", "contract_qty", "lump_amount")
                   if getattr(old, f) != d[f]]
        used = repo.item_used_in_approved(self.conn, item_id)
        if used and old.method != d["method"]:
            raise ConflictError("Onaylı hakedişte kullanılmış kalemin yöntemi değiştirilemez.")
        if used and changes and not clean(reason):
            raise ValidationError("Onaylı hakedişte kullanılmış kalemin fiyat/miktar/bedel değişikliği için gerekçe "
                                  "(onaylı değişiklik, yeni birim fiyat tutanağı vb.) girilmelidir.")
        with transaction(self.conn):
            repo.update_item(self.conn, item_id, d)
            for f, a, b in changes:
                if used or clean(reason):
                    repo.insert_item_change(self.conn, item_id, f, None if a is None else str(a),
                                            None if b is None else str(b), clean(reason) or "Taslak düzeltme",
                                            clean(ref))
            audit.log(self.conn, "pp_item", item_id, "GUNCELLE", old={f: a for f, a, _ in changes},
                      new={f: b for f, _, b in changes}, note=clean(reason))

    def delete_item(self, item_id: int) -> None:
        if repo.get_item(self.conn, item_id) is None:
            raise NotFoundError("Kalem bulunamadı.")
        if repo.item_used(self.conn, item_id):
            raise ConflictError("Hakedişte kullanılmış kalem silinemez.")
        with transaction(self.conn):
            repo.delete_item(self.conn, item_id)
            audit.log(self.conn, "pp_item", item_id, "SIL")

    def item_changes(self, contract_id: int):
        return repo.item_changes(self.conn, contract_id)

    def import_from_estimate(self, contract_id: int, estimate_id: int, bid_id: int | None = None) -> int:
        """Keşif kalemlerini sözleşme kalemi olarak aktarır. Fiyat: istekli teklifi (bid_id) verilirse teklifin birim
        fiyatı / götürü tutarı, verilmezse yaklaşık maliyet birim fiyatı. Kalem zaten varsa eklenmez."""
        from yapimmaliyet.services.tender_service import TenderService

        c = self.contract(contract_id)
        view = TenderService(self.conn).view(estimate_id)
        bid = tender_bids.lines(self.conn, bid_id) if bid_id else {}
        existing = {(i.code or "", i.description) for i in self.items(contract_id)}
        added, line_no = 0, repo.next_line_no(self.conn, contract_id)
        with transaction(self.conn):
            for ln in view.lines:
                if (ln.code or "", ln.description or "") in existing or not ln.description:
                    continue
                method = {"BIRIM_FIYAT": "BIRIM_FIYAT", "ANAHTAR_TESLIM": "GOTURU"}.get(c.model) or ln.method or \
                    "BIRIM_FIYAT"
                up, lump = bid.get(ln.item_id, (None, None))
                d = {"line_no": line_no, "code": ln.code, "description": ln.description, "unit": ln.unit,
                     "discipline": ln.discipline, "group_name": None, "method": method, "source": "SOZLESME",
                     "estimate_item_id": ln.item_id}
                if method == "GOTURU":
                    d["lump_amount"] = lump if lump is not None else (
                        up * ln.quantity if up is not None else ln.ym_amount) or Decimal(0)
                    d["unit"] = "%"
                else:
                    d["unit_price"] = up if up is not None else ln.ym_unit_price
                    d["contract_qty"] = ln.quantity
                repo.insert_item(self.conn, contract_id, d)
                line_no += 1
                added += 1
            audit.log(self.conn, "pp_contract", contract_id, "TOPLU", new={"estimate": estimate_id, "bid": bid_id,
                                                                          "added": added}, note="Keşiften kalem aktarımı")
        return added

    # --- kesinti türleri ---------------------------------------------------------
    def deduction_types(self, contract_id: int, active_only: bool = True):
        return repo.list_deduction_types(self.conn, contract_id, active_only)

    def deduction_type_rows(self, contract_id: int):
        return repo.deduction_type_rows(self.conn, contract_id)

    @staticmethod
    def _dtype(name, base, rate, is_advance) -> tuple[str, Decimal | None]:
        name = require(name, "Kesinti adı")
        if base not in DEDUCTION_BASES:
            raise ValidationError("Geçersiz kesinti tabanı.")
        r = None if base == "MANUEL" else _dec(rate, f"'{name}' oranı")
        if r is not None and r > 1:
            raise ValidationError(f"'{name}' oranı kesir olarak girilmelidir (ör. %5 için 0,05).")
        if is_advance and base != "MANUEL":
            raise ValidationError("Avans mahsubu tutar olarak girilen (MANUEL) bir kesinti olmalıdır.")
        return name, r

    def add_deduction_type(self, contract_id: int, name: str, base: str, rate=None, is_advance: bool = False,
                           is_guarantee: bool = False, basis_note: str | None = None) -> int:
        self.contract(contract_id)
        name, r = self._dtype(name, base, rate, is_advance)
        order = len(repo.deduction_type_rows(self.conn, contract_id)) + 1
        try:
            with transaction(self.conn):
                return repo.insert_deduction_type(self.conn, contract_id, name, base, r, is_advance, is_guarantee,
                                                  order, clean(basis_note))
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"'{name}' adlı kesinti zaten tanımlı.") from exc

    def update_deduction_type(self, type_id: int, name: str, base: str, rate=None, is_advance: bool = False,
                              is_guarantee: bool = False, active: bool = True, basis_note: str | None = None) -> None:
        name, r = self._dtype(name, base, rate, is_advance)
        row = self.conn.execute("SELECT * FROM pp_deduction_types WHERE id = ?", (type_id,)).fetchone()
        if row is None:
            raise NotFoundError("Kesinti türü bulunamadı.")
        if repo.deduction_type_used(self.conn, type_id) and row["base"] != base:
            raise ConflictError("Kullanılmış kesinti türünün tabanı değiştirilemez; yeni tür tanımlayın.")
        with transaction(self.conn):
            repo.update_deduction_type(self.conn, type_id, name, base, r, is_advance, is_guarantee, active,
                                       clean(basis_note))
            audit.log(self.conn, "pp_deduction_type", type_id, "GUNCELLE", old={"rate": row["rate"]},
                      new={"rate": r, "active": active})

    def delete_deduction_type(self, type_id: int) -> None:
        if repo.deduction_type_used(self.conn, type_id):
            raise ConflictError("Hakedişte kullanılmış kesinti türü silinemez; pasifleştirin.")
        with transaction(self.conn):
            repo.delete_deduction_type(self.conn, type_id)

    def add_preset_deductions(self, contract_id: int) -> int:
        """Türüne göre örnek hakedişlerden alınmış kesinti türlerini (yoksa) ekler; hepsi düzenlenebilir."""
        c = self.contract(contract_id)
        have = {r["name"] for r in repo.deduction_type_rows(self.conn, contract_id)}
        n = 0
        for name, base, rate, adv, guar, src in PRESETS[c.kind]:
            if name in have:
                continue
            self.add_deduction_type(contract_id, name, base, rate, adv, guar, PRESET_NOTE.format(src=src))
            n += 1
        return n
