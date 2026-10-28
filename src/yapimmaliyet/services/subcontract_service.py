"""Alt yüklenici iş paketleri ve teklif karşılaştırması.

Akış: proje → iş paketi (keşif kalemlerinden ya da elle; disiplin ve kapsam) → teklif istenecek firmalar →
firma teklifleri (satır birim fiyatı, dahil/hariç işler, termin, ödeme, KDV, revizyon) → karşılaştırma → karar →
alt yüklenici (TASERON) sözleşmesi.

Kurallar:
- Teklifler tekil firma kimliğine (parties) bağlıdır; aynı pakette aynı firmanın revizyonu eskisini REVIZE_EDILDI
  yapar (silmez). Geçerli teklif firma başına tektir.
- Tutar = Σ round(miktar × KDV hariç birim fiyat, 2). KDV dahil girilen fiyat KDV oranıyla hariç tutara çevrilir.
- Karşılaştırma toplamı yalnız kapsamı tam teklifleri "tam" sayar; fiyatsız ya da kapsam dışı kalem varsa teklif
  "kapsam eksik" işaretlenir ve toplamlar kıyaslanabilir değildir. En düşük fiyat otomatik seçilmez; karar kullanıcıdadır.
- Kapsamı eksik teklif seçilirken gerekçe ve açık onay (accept_scope_gap) gerekir.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.progress_service import ProgressService
from yapimmaliyet.services.validators import clean, require, validate_iso_date, validate_quantity

PACKAGE_STATUSES = {"TASLAK": "Taslak", "TEKLIF_ISTENDI": "Teklif istendi", "DEGERLENDIRMEDE": "Değerlendirmede",
                    "SECILDI": "Teklif seçildi", "IPTAL": "İptal"}
ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class PackageItem:
    id: int
    line_no: int
    code: str
    description: str
    unit: str
    quantity: Decimal


@dataclass(frozen=True)
class BidColumn:
    bid_id: int
    party_id: int
    firm: str
    revision: int
    bid_date: str | None
    valid_until: str | None
    delivery_days: int | None
    payment_terms: str
    vat_included: bool
    inclusions: str
    exclusions: str
    notes: str
    amounts: dict[int, Decimal | None]       # kalem → KDV hariç tutar (fiyatsız: None)
    unit_prices: dict[int, Decimal | None]   # kalem → KDV hariç birim fiyat
    out_of_scope: list[int]
    missing: list[int]
    total: Decimal
    expired: bool

    @property
    def complete(self) -> bool:
        return not self.missing and not self.out_of_scope


@dataclass(frozen=True)
class HistoryRow:
    bid_id: int
    date: str
    firm: str
    project: str
    package: str
    unit: str
    quantity: Decimal
    unit_price: Decimal            # KDV hariç
    revision: int
    bid_status: str
    won: bool
    in_scope: bool
    equivalent: bool               # aynı poz/tanım VE aynı birim
    age_days: int | None
    notes: tuple[str, ...]

    @property
    def comparable(self) -> bool:
        """Doğrudan karşılaştırılabilir: eşdeğer, kapsamda, geçerli teklif ve 12 aydan eski değil."""
        return self.equivalent and self.in_scope and self.bid_status == "GECERLI" and (self.age_days or 0) <= 365


@dataclass
class Comparison:
    package_id: int
    items: list[PackageItem]
    bids: list[BidColumn]
    lowest_item: dict[int, int] = field(default_factory=dict)   # kalem → en düşük fiyatlı teklif kimliği
    lowest_complete: int | None = None                           # yalnız bilgi: kapsamı tam teklifler arasında

    @property
    def comparable(self) -> bool:
        """Bütün teklifler tam kapsamlıysa toplamlar doğrudan kıyaslanabilir."""
        return bool(self.bids) and all(b.complete for b in self.bids)


def _money(value, label: str, allow_zero: bool = False) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçerli bir sayı değil.") from exc
    if d < 0 or (d == 0 and not allow_zero):
        raise ValidationError(f"{label} sıfırdan büyük olmalıdır.")
    return d


class SubcontractService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- paket ----------------------------------------------------------------
    def packages(self, project_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT k.*, (SELECT COUNT(*) FROM sub_package_items i WHERE i.package_id = k.id) AS item_count,"
               " (SELECT COUNT(*) FROM sub_bids b WHERE b.package_id = k.id AND b.status = 'GECERLI') AS bid_count"
               " FROM sub_packages k")
        if project_id is not None:
            return self.conn.execute(sql + " WHERE k.project_id = ? ORDER BY k.id DESC", (project_id,)).fetchall()
        return self.conn.execute(sql + " ORDER BY k.id DESC").fetchall()

    def package(self, package_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM sub_packages WHERE id = ?", (package_id,)).fetchone()
        if r is None:
            raise NotFoundError("İş paketi bulunamadı.")
        return r

    def _editable(self, package_id: int) -> sqlite3.Row:
        r = self.package(package_id)
        if r["status"] in ("SECILDI", "IPTAL"):
            raise ConflictError("Teklifi seçilmiş ya da iptal edilmiş paket değiştirilemez.")
        return r

    def create_package(self, project_id: int, name: str, discipline: str | None = None, scope: str = "",
                       estimate_id: int | None = None, estimate_item_ids: list[int] | None = None) -> int:
        if self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise ValidationError("Proje bulunamadı.")
        if discipline and discipline not in DISCIPLINES:
            raise ValidationError("Geçersiz disiplin.")
        with transaction(self.conn):
            pid = self.conn.execute(
                "INSERT INTO sub_packages(project_id, estimate_id, name, discipline, scope) VALUES (?, ?, ?, ?, ?)",
                (project_id, estimate_id, require(name, "Paket adı"), discipline or None, clean(scope) or "")).lastrowid
            for n, item_id in enumerate(estimate_item_ids or [], 1):
                r = self.conn.execute("SELECT * FROM estimate_items WHERE id = ?", (item_id,)).fetchone()
                if r is None or (estimate_id is not None and r["estimate_id"] != estimate_id):
                    raise ValidationError("Seçilen keşif kalemi bu keşfe ait değil.")
                self.conn.execute(
                    "INSERT INTO sub_package_items(package_id, line_no, estimate_item_id, code, description, unit,"
                    " quantity) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pid, n, item_id, r["raw_code"] or "", r["raw_description"] or "", r["raw_unit"] or "", r["quantity"]))
            audit.log(self.conn, "sub_package", pid, "EKLE", new={"project": project_id, "name": name})
        return pid

    def add_item(self, package_id: int, description: str, unit: str, quantity, code: str = "") -> int:
        self._editable(package_id)
        q = validate_quantity(quantity)
        if q <= 0:
            raise ValidationError("Miktar sıfırdan büyük olmalıdır.")
        with transaction(self.conn):
            n = self.conn.execute("SELECT COALESCE(MAX(line_no), 0) + 1 FROM sub_package_items WHERE package_id = ?",
                                  (package_id,)).fetchone()[0]
            return self.conn.execute(
                "INSERT INTO sub_package_items(package_id, line_no, code, description, unit, quantity)"
                " VALUES (?, ?, ?, ?, ?, ?)", (package_id, n, clean(code) or "", require(description, "Kalem tanımı"),
                                               require(unit, "Birim"), to_db_text(q, 3))).lastrowid

    def duplicate_item(self, item_id: int) -> int:
        """Paket kaleminin kopyasını paketin sonuna ekler (keşif kalemi bağı ve firma fiyatları kopyalanmaz)."""
        r = self.conn.execute("SELECT package_id, code, description, unit, quantity FROM sub_package_items WHERE id = ?",
                              (item_id,)).fetchone()
        if r is None:
            raise NotFoundError("Paket kalemi bulunamadı.")
        return self.add_item(r["package_id"], r["description"], r["unit"], from_db_text(r["quantity"]), r["code"] or "")

    def items(self, package_id: int) -> list[PackageItem]:
        return [PackageItem(r["id"], r["line_no"], r["code"], r["description"], r["unit"], from_db_text(r["quantity"]))
                for r in self.conn.execute("SELECT * FROM sub_package_items WHERE package_id = ? ORDER BY line_no",
                                           (package_id,))]

    def invite(self, package_id: int, party_id: int, invited_on: str | None = None) -> None:
        self._editable(package_id)
        if self.conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
            raise ValidationError("Firma bulunamadı.")
        with transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO sub_invites(package_id, party_id, invited_on) VALUES (?, ?, ?)",
                              (package_id, party_id, validate_iso_date(invited_on, "Teklif isteme tarihi")))
            self.conn.execute("UPDATE sub_packages SET status = 'TEKLIF_ISTENDI' WHERE id = ? AND status = 'TASLAK'",
                              (package_id,))

    def invited(self, package_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.*, p.name FROM sub_invites i JOIN parties p ON p.id = i.party_id WHERE i.package_id = ?"
            " ORDER BY p.name", (package_id,)).fetchall()

    # --- teklif ---------------------------------------------------------------
    def _bid_values(self, v: dict) -> dict:
        vat = _money(v.get("vat_rate", 20), "KDV oranı", allow_zero=True)
        if vat >= HUNDRED:
            raise ValidationError("KDV oranı yüzde olarak girilmelidir (ör. 20).")
        days = v.get("delivery_days")
        if days not in (None, "") and int(days) < 0:
            raise ValidationError("Termin gün sayısı negatif olamaz.")
        bd = validate_iso_date(v.get("bid_date"), "Teklif tarihi")
        vu = validate_iso_date(v.get("valid_until"), "Geçerlilik tarihi")
        if bd and vu and vu < bd:
            raise ValidationError("Geçerlilik tarihi teklif tarihinden önce olamaz.")
        return dict(bid_no=clean(v.get("bid_no")) or "", bid_date=bd, valid_until=vu,
                    delivery_days=int(days) if days not in (None, "") else None,
                    payment_terms=clean(v.get("payment_terms")) or "", vat_included=int(bool(v.get("vat_included"))),
                    vat_rate=to_db_text(vat, 2), inclusions=clean(v.get("inclusions")) or "",
                    exclusions=clean(v.get("exclusions")) or "", notes=clean(v.get("notes")) or "",
                    file_path=clean(v.get("file_path")) or "")

    def _write_lines(self, package_id: int, bid_id: int, lines: dict) -> None:
        valid = {i.id for i in self.items(package_id)}
        for item_id, raw in lines.items():
            if item_id not in valid:
                raise ValidationError("Teklif satırı pakette olmayan bir kaleme bağlanamaz.")
            price, in_scope, note = (raw if isinstance(raw, tuple) else (raw, True, ""))
            if price in (None, ""):
                continue
            self.conn.execute("INSERT INTO sub_bid_lines(bid_id, item_id, unit_price, in_scope, note) VALUES (?, ?, ?, ?, ?)",
                              (bid_id, item_id, to_db_text(_money(price, "Birim fiyat", allow_zero=True), 4),
                               int(bool(in_scope)), clean(note) or ""))

    def add_bid(self, package_id: int, party_id: int, lines: dict, **fields) -> int:
        """`lines`: {kalem kimliği: birim fiyat | (birim fiyat, kapsamda_mı, not)}. Alanlar: bid_no, bid_date,
        valid_until, delivery_days, payment_terms, vat_included, vat_rate, inclusions, exclusions, notes."""
        self._editable(package_id)
        if self.conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
            raise ValidationError("Firma bulunamadı.")
        if self.conn.execute("SELECT 1 FROM sub_bids WHERE package_id = ? AND party_id = ? AND status = 'GECERLI'",
                             (package_id, party_id)).fetchone():
            raise ConflictError("Bu firmanın pakette geçerli bir teklifi var; değişiklik için revizyon açın.")
        v = self._bid_values(fields)
        with transaction(self.conn):
            rev = self.conn.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM sub_bids WHERE package_id = ? AND "
                                    "party_id = ?", (package_id, party_id)).fetchone()[0]
            cols = ["package_id", "party_id", "revision"] + list(v)
            bid = self.conn.execute(f"INSERT INTO sub_bids({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    [package_id, party_id, rev] + list(v.values())).lastrowid
            self._write_lines(package_id, bid, lines)
            self.conn.execute("UPDATE sub_packages SET status = 'DEGERLENDIRMEDE' WHERE id = ? AND status IN "
                              "('TASLAK', 'TEKLIF_ISTENDI')", (package_id,))
            audit.log(self.conn, "sub_bid", bid, "EKLE", new={"package": package_id, "party": party_id, "revision": rev})
        return bid

    def preview_import(self, package_id: int, path):
        """Excel / CSV / metin tabanlı PDF teklif dosyasını okur ve paketle eşleştirir; hiçbir şey kaydetmez."""
        from yapimmaliyet.services.subcontract_import import read_bid_file

        self.package(package_id)
        return read_bid_file(path, self.items(package_id))

    def import_bid(self, package_id: int, party_id: int, path, accept_gaps: bool = False, **fields) -> int:
        """Excel dosyasından teklif kaydeder. Eşleşmeyen/yinelenen satır ya da dosyada olmayan kalem varsa
        `accept_gaps` açıkça verilmedikçe reddeder (sessiz veri kaybı yok); teklif tek işlemde yazılır."""
        res = self.preview_import(package_id, path)
        if not res.lines:
            raise ValidationError("Dosyadan eşleşen hiçbir teklif satırı alınamadı. " + res.summary())
        if not res.clean and not accept_gaps:
            raise ValidationError("Dosya paketle birebir uyuşmuyor; kapsam farkını onaylamadan aktarılamaz. " +
                                  res.summary())
        fields.setdefault("file_path", str(path))
        return self.add_bid(package_id, party_id, dict(res.lines), **fields)

    def revise_bid(self, bid_id: int, lines: dict, **fields) -> int:
        old = self.bid_row(bid_id)
        self._editable(old["package_id"])
        if old["status"] != "GECERLI":
            raise ConflictError("Yalnız geçerli teklif revize edilir.")
        merged = {k: old[k] for k in ("bid_no", "bid_date", "valid_until", "delivery_days", "payment_terms",
                                      "vat_included", "vat_rate", "inclusions", "exclusions", "notes", "file_path")}
        merged.update(fields)
        v = self._bid_values(merged)
        with transaction(self.conn):
            self.conn.execute("UPDATE sub_bids SET status = 'REVIZE_EDILDI' WHERE id = ?", (bid_id,))
            cols = ["package_id", "party_id", "revision", "supersedes_id"] + list(v)
            new = self.conn.execute(f"INSERT INTO sub_bids({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                    [old["package_id"], old["party_id"], old["revision"] + 1, bid_id] +
                                    list(v.values())).lastrowid
            self._write_lines(old["package_id"], new, lines)
            audit.log(self.conn, "sub_bid", new, "EKLE", new={"supersedes": bid_id, "revision": old["revision"] + 1})
        return new

    def delete_package(self, package_id: int) -> None:
        """İş paketini kalemleri, davetleri ve teklifleriyle siler. Teklifi seçilmiş (sözleşmeye bağlanabilir) paket silinemez."""
        p = self.package(package_id)
        if p["status"] == "SECILDI":
            raise ConflictError("Teklifi seçilmiş paket silinemez. Önce seçimi geri alın ya da paketi iptal edin.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM sub_packages WHERE id = ?", (package_id,))
            audit.log(self.conn, "sub_package", package_id, "SIL", old={"name": p["name"]})

    def delete_item(self, item_id: int) -> None:
        r = self.conn.execute("SELECT package_id, description FROM sub_package_items WHERE id = ?", (item_id,)).fetchone()
        if r is None:
            raise NotFoundError("Kalem bulunamadı.")
        self._editable(r["package_id"])
        with transaction(self.conn):
            self.conn.execute("DELETE FROM sub_package_items WHERE id = ?", (item_id,))
            audit.log(self.conn, "sub_package_item", item_id, "SIL", old={"description": r["description"]})

    def delete_bid(self, bid_id: int) -> None:
        """Teklifi (revizyonunu) siler; yerini aldığı önceki revizyon yeniden geçerli olur."""
        b = self.bid_row(bid_id)
        self._editable(b["package_id"])
        with transaction(self.conn):
            if b["supersedes_id"]:
                self.conn.execute("UPDATE sub_bids SET status = 'GECERLI' WHERE id = ?", (b["supersedes_id"],))
            self.conn.execute("DELETE FROM sub_bids WHERE id = ?", (bid_id,))
            audit.log(self.conn, "sub_bid", bid_id, "SIL", old={"revision": b["revision"]})

    def withdraw_bid(self, bid_id: int) -> None:
        b = self.bid_row(bid_id)
        self._editable(b["package_id"])
        with transaction(self.conn):
            self.conn.execute("UPDATE sub_bids SET status = 'GERI_CEKILDI' WHERE id = ?", (bid_id,))
            audit.log(self.conn, "sub_bid", bid_id, "GUNCELLE", new={"status": "GERI_CEKILDI"})

    def bid_row(self, bid_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM sub_bids WHERE id = ?", (bid_id,)).fetchone()
        if r is None:
            raise NotFoundError("Teklif bulunamadı.")
        return r

    def bid_lines(self, bid_id: int) -> dict[int, tuple[Decimal, bool, str]]:
        return {r["item_id"]: (from_db_text(r["unit_price"]), bool(r["in_scope"]), r["note"])
                for r in self.conn.execute("SELECT * FROM sub_bid_lines WHERE bid_id = ?", (bid_id,))}

    # --- karşılaştırma ----------------------------------------------------------
    def compare(self, package_id: int, today: str | None = None) -> Comparison:
        today = today or date.today().isoformat()
        items = self.items(package_id)
        cmp = Comparison(package_id, items, [])
        for b in self.conn.execute(
                "SELECT b.*, p.name FROM sub_bids b JOIN parties p ON p.id = b.party_id WHERE b.package_id = ? AND "
                "b.status = 'GECERLI' ORDER BY b.id", (package_id,)):
            lines = self.bid_lines(b["id"])
            vat_factor = 1 + from_db_text(b["vat_rate"]) / HUNDRED if b["vat_included"] else Decimal(1)
            amounts, prices, out, missing = {}, {}, [], []
            for it in items:
                ln = lines.get(it.id)
                if ln is None:
                    amounts[it.id] = prices[it.id] = None
                    missing.append(it.id)
                    continue
                price = ln[0] / vat_factor
                prices[it.id] = price
                if not ln[1]:
                    out.append(it.id)
                    amounts[it.id] = None
                    continue
                amounts[it.id] = quantize(it.quantity * price, 2)
            cmp.bids.append(BidColumn(
                b["id"], b["party_id"], b["name"], b["revision"], b["bid_date"], b["valid_until"], b["delivery_days"],
                b["payment_terms"], bool(b["vat_included"]), b["inclusions"], b["exclusions"], b["notes"], amounts,
                prices, out, missing, sum((a for a in amounts.values() if a is not None), ZERO),
                bool(b["valid_until"] and b["valid_until"] < today)))
        for it in items:
            priced = [(c.unit_prices[it.id], c.bid_id) for c in cmp.bids
                      if c.unit_prices[it.id] is not None and it.id not in c.out_of_scope]
            if priced:
                cmp.lowest_item[it.id] = min(priced)[1]
        full = [c for c in cmp.bids if c.complete]
        if full:
            cmp.lowest_complete = min(full, key=lambda c: c.total).bid_id
        return cmp

    # --- karar ve sözleşme -------------------------------------------------------
    def decide(self, package_id: int, bid_id: int, note: str, decided_by: str, accept_scope_gap: bool = False) -> None:
        authz.require("TEKLIF", authz.APPROVE, "alt yüklenici teklif seçimi")
        self._editable(package_id)
        b = next((c for c in self.compare(package_id).bids if c.bid_id == bid_id), None)
        if b is None:
            raise ValidationError("Seçilen teklif bu paketin geçerli bir teklifi değil.")
        if not b.complete and not accept_scope_gap:
            raise ValidationError("Teklifte fiyatsız ya da kapsam dışı kalem var; seçmek için kapsam farkını açıkça "
                                  "onaylayın.")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE sub_packages SET status = 'SECILDI', selected_bid_id = ?, decision_note = ?, decided_by = ?,"
                " decided_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime') WHERE id = ?",
                (bid_id, require(note, "Seçim gerekçesi"), require(decided_by, "Karar veren"), package_id))
            audit.log(self.conn, "sub_package", package_id, "GUNCELLE", new={"selected_bid": bid_id},
                      note="Teklif seçildi")

    def create_contract(self, package_id: int, employer: str, contract_no: str | None = None,
                        contract_date: str | None = None, start_date: str | None = None,
                        end_date: str | None = None) -> int:
        """Seçilen teklifi alt yüklenici (TASERON) sözleşmesine bağlar; kalemler teklif birim fiyatıyla açılır."""
        pk = self.package(package_id)
        if pk["status"] != "SECILDI" or pk["selected_bid_id"] is None:
            raise ConflictError("Sözleşme için önce teklif seçilmelidir.")
        if self.conn.execute("SELECT 1 FROM pp_contracts WHERE sub_bid_id = ?", (pk["selected_bid_id"],)).fetchone():
            raise ConflictError("Bu teklif için sözleşme zaten açılmış.")
        col = next(c for c in self.compare(package_id).bids if c.bid_id == pk["selected_bid_id"])
        party = self.conn.execute("SELECT name FROM parties WHERE id = ?", (col.party_id,)).fetchone()["name"]
        vat = from_db_text(self.bid_row(col.bid_id)["vat_rate"]) / HUNDRED
        ps = ProgressService(self.conn)
        cid = ps.create_contract({
            "kind": "TASERON", "project_id": pk["project_id"], "title": pk["name"], "employer": employer,
            "contractor": party, "contract_no": contract_no, "contract_date": contract_date, "start_date": start_date,
            "end_date": end_date, "model": "BIRIM_FIYAT", "contract_amount": str(col.total),
            "vat_rate": str(vat), "notes": f"Alt yüklenici teklifi #{col.bid_id} (rev. {col.revision})"})
        for it in self.items(package_id):
            if col.amounts.get(it.id) is None:
                continue
            ps.add_item(cid, {"code": it.code, "description": it.description, "unit": it.unit,
                              "contract_qty": str(it.quantity), "unit_price": str(quantize(col.unit_prices[it.id], 4)),
                              "method": "BIRIM_FIYAT", "discipline": pk["discipline"]})
        with transaction(self.conn):
            self.conn.execute("UPDATE pp_contracts SET sub_bid_id = ? WHERE id = ?", (col.bid_id, cid))
            audit.log(self.conn, "pp_contract", cid, "GUNCELLE", new={"sub_bid_id": col.bid_id},
                      note="Alt yüklenici teklifinden sözleşme")
        return cid

    # --- eşdeğer kalem fiyat geçmişi ----------------------------------------------
    def price_history(self, item_id: int, today: str | None = None) -> list[HistoryRow]:
        """Seçili paket kaleminin diğer projelerdeki/paketlerdeki teklif fiyatları.

        Eşdeğerlik: aynı poz kodu ya da aynı normalize tanım VE aynı birim. Birimi farklı kalemler listelenir ama
        eşdeğer sayılmaz; 12 aydan eski, kapsam dışı, revize edilmiş ya da geri çekilmiş teklifler karşılaştırılabilir
        sayılmaz (notla işaretlenir). KDV dahil girilen fiyatlar KDV hariça çevrilir.
        """
        from yapimmaliyet.domain.text_tr import normalize_search

        today = today or date.today().isoformat()
        it = self.conn.execute("SELECT * FROM sub_package_items WHERE id = ?", (item_id,)).fetchone()
        if it is None:
            raise NotFoundError("Paket kalemi bulunamadı.")
        key_code, key_desc = normalize_search(it["code"] or "").strip(), normalize_search(it["description"]).strip()
        out = []
        for r in self.conn.execute(
                "SELECT l.unit_price, l.in_scope, b.id AS bid_id, b.revision, b.status, b.bid_date, b.created_at,"
                " b.vat_included, b.vat_rate, b.package_id, p.name AS firm, pr.name AS project, k.name AS package,"
                " k.selected_bid_id, i.code, i.description, i.unit, i.quantity, i.id AS item_id"
                " FROM sub_bid_lines l JOIN sub_bids b ON b.id = l.bid_id JOIN sub_package_items i ON i.id = l.item_id"
                " JOIN sub_packages k ON k.id = b.package_id JOIN parties p ON p.id = b.party_id"
                " JOIN projects pr ON pr.id = k.project_id WHERE l.item_id <> ? ORDER BY b.id DESC", (item_id,)):
            same_code = bool(key_code) and normalize_search(r["code"] or "").strip() == key_code
            same_desc = normalize_search(r["description"]).strip() == key_desc
            if not (same_code or same_desc):
                continue
            same_unit = normalize_search(r["unit"]).strip() == normalize_search(it["unit"]).strip()
            d = r["bid_date"] or r["created_at"][:10]
            age = (date.fromisoformat(today) - date.fromisoformat(d)).days
            price = from_db_text(r["unit_price"])
            if r["vat_included"]:
                price = price / (1 + from_db_text(r["vat_rate"]) / HUNDRED)
            notes = []
            if not same_unit:
                notes.append(f"birim farklı ({r['unit']} ≠ {it['unit']}) — eşdeğer sayılmadı")
            if age > 365:
                notes.append(f"{age // 30} ay önceki fiyat — tarih farkı")
            if not r["in_scope"]:
                notes.append("kapsam dışı işaretli")
            if r["status"] != "GECERLI":
                notes.append("revize edilmiş / geri çekilmiş teklif")
            out.append(HistoryRow(r["bid_id"], d, r["firm"], r["project"], r["package"], r["unit"],
                                  from_db_text(r["quantity"]), quantize(price, 2), r["revision"], r["status"],
                                  r["selected_bid_id"] == r["bid_id"], bool(r["in_scope"]), same_unit, age, tuple(notes)))
        return out

    # --- firma geçmişi --------------------------------------------------------
    def firm_history(self, party_id: int) -> list[sqlite3.Row]:
        """Firmanın bütün alt yüklenici teklifleri (revizyonlar dahil) ve sonuçları — tekil firma kimliğiyle."""
        return self.conn.execute(
            "SELECT b.id AS bid_id, b.revision, b.status AS bid_status, b.bid_date, b.valid_until, b.vat_included,"
            " k.id AS package_id, k.name AS package, k.discipline, k.status AS package_status, k.project_id,"
            " pr.name AS project, (k.selected_bid_id = b.id) AS won,"
            " (SELECT id FROM pp_contracts c WHERE c.sub_bid_id = b.id) AS contract_id"
            " FROM sub_bids b JOIN sub_packages k ON k.id = b.package_id JOIN projects pr ON pr.id = k.project_id"
            " WHERE b.party_id = ? ORDER BY b.id DESC", (party_id,)).fetchall()
