"""Satın alma fiyat mukayesesi ve onay servisi.

- Talep (iş/konu/kapsam) → kalemler → firma teklifleri (satırlar) → değerlendirme ölçütleri/puanlar → karar.
- Teklif revizyonu yeni kayıt oluşturur; eski revizyon "revize edildi" olarak saklanır.
- Karar (seçilen teklif) gerekçe ve onaylayan bilgisi olmadan kaydedilmez; onaylanan talep değiştirilemez.
- Teklif fiyatı kütüphaneye yazılmaz; istenirse kaynağı belirtilmiş ÖZEL poz olarak oluşturulur.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, parties
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.purchase import Evaluation, Offer, OfferLine, RequestItem, evaluate
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require, validate_iso_date, validate_price, validate_quantity

OFFER_FIELDS = ("contact", "offer_no", "offer_date", "valid_until", "vat_rate", "shipping", "installation",
                "other_costs", "delivery_days", "delivery_terms", "payment_terms", "warranty", "technical_ok",
                "technical_note", "file_path")
MONEY_FIELDS = ("shipping", "installation", "other_costs")


@dataclass(frozen=True)
class PurchaseRequest:
    id: int
    project_id: int | None
    title: str
    scope: str
    eval_date: str | None
    status: str
    selected_offer_id: int | None
    decision_reason: str | None
    evaluator: str | None
    approver: str | None
    decided_at: str | None
    revision: int
    created_at: str


def _pct(value, field: str) -> Decimal:
    d = validate_price(value)
    if d > 100:
        raise ValidationError(f"{field} %0 ile %100 arasında olmalıdır.")
    return d


class PurchaseService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path | None = None):
        self.conn = conn
        self.data_dir = Path(data_dir) if data_dir else None

    # --- teklif dosyası (gerçek PDF / tarama; içe aktarılmaz, yalnız saklanır ve açılır) -------------------
    def store_offer_file(self, src: Path | str) -> str:
        """Dosyayı veri klasörüne kopyalar (aynı içerik tek kez) ve veri klasörüne göre göreli yolunu döndürür."""
        if self.data_dir is None:
            raise ValidationError("Veri klasörü tanımlı değil; teklif dosyası saklanamadı.")
        src = Path(src)
        if not src.is_file():
            raise ValidationError(f"Teklif dosyası bulunamadı: {src}")
        h = hashlib.sha256()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        safe = re.sub(r"[^\w.\- ]+", "_", src.name, flags=re.UNICODE).strip() or "teklif"
        rel = f"KaynakDosyalar/Teklifler/{h.hexdigest()[:12]}_{safe}"
        dest = self.data_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        return rel

    def offer_file(self, oid: int) -> Path | None:
        """Teklife eklenmiş dosyanın tam yolu (yoksa ya da bulunamıyorsa None)."""
        rel = self._offer_row(oid)["file_path"]
        if not rel:
            return None
        p = Path(rel)
        if not p.is_absolute() and self.data_dir is not None:
            p = self.data_dir / rel
        return p if p.is_file() else None

    # --- talep --------------------------------------------------------------
    def create_request(self, title: str, scope: str = "", project_id: int | None = None,
                       eval_date: str | None = None) -> int:
        with transaction(self.conn):
            rid = self.conn.execute(
                "INSERT INTO purchase_requests(title, scope, project_id, eval_date) VALUES (?, ?, ?, ?)",
                (require(title, "Satın alma konusu"), clean(scope) or "", project_id,
                 validate_iso_date(eval_date, "Değerlendirme tarihi"))).lastrowid
            audit.log(self.conn, "purchase_request", rid, "EKLE", new={"title": title})
        return rid

    def list_requests(self) -> list[PurchaseRequest]:
        return [PurchaseRequest(*r) for r in self.conn.execute(
            "SELECT id, project_id, title, scope, eval_date, status, selected_offer_id, decision_reason, evaluator,"
            " approver, decided_at, revision, created_at FROM purchase_requests ORDER BY id DESC")]

    def get_request(self, rid: int) -> PurchaseRequest:
        r = next((x for x in self.list_requests() if x.id == rid), None)
        if r is None:
            raise NotFoundError("Satın alma talebi bulunamadı.")
        return r

    def _editable(self, rid: int) -> PurchaseRequest:
        r = self.get_request(rid)
        if r.status != "TASLAK":
            raise ConflictError("Onaylanmış/iptal edilmiş talep değiştirilemez. Değişiklik için yeni revizyon açın.")
        return r

    def delete_request(self, rid: int) -> None:
        self._editable(rid)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM purchase_requests WHERE id = ?", (rid,))
            audit.log(self.conn, "purchase_request", rid, "SIL")

    def delete_item(self, item_id: int) -> None:
        """Talep kalemini siler (taslak talepte); bu kaleme verilmiş teklif satırları da gider."""
        r = self.conn.execute("SELECT request_id, description FROM purchase_items WHERE id = ?", (item_id,)).fetchone()
        if r is None:
            raise NotFoundError("Kalem bulunamadı.")
        self._editable(r["request_id"])
        with transaction(self.conn):
            self.conn.execute("DELETE FROM purchase_items WHERE id = ?", (item_id,))
            audit.log(self.conn, "purchase_item", item_id, "SIL", old={"description": r["description"]})

    def delete_offer(self, offer_id: int) -> None:
        """Firma teklifini (revizyonunu) siler (taslak talepte). Silinen revizyonun yerini alan önceki revizyon yeniden geçerli olur."""
        o = self._offer_row(offer_id)
        self._editable(o["request_id"])
        with transaction(self.conn):
            if o["supersedes_id"]:
                self.conn.execute("UPDATE purchase_offers SET status = 'GECERLI' WHERE id = ?", (o["supersedes_id"],))
            self.conn.execute("DELETE FROM purchase_offers WHERE id = ?", (offer_id,))
            audit.log(self.conn, "purchase_offer", offer_id, "SIL", old={"firm": o["firm"], "revision": o["revision"]})

    # --- kalemler -------------------------------------------------------------
    def add_item(self, rid: int, description: str, unit: str, quantity, spec: str = "",
                 position: dict | None = None) -> int:
        """`position`: {"kind": "RESMI"|"OZEL", "ref_id", "code", "source"} — kalemin bağlı olduğu poz (isteğe bağlı)."""
        self._editable(rid)
        pos = position or {}
        if pos and pos.get("kind") not in ("RESMI", "OZEL"):
            raise ValidationError("Poz türü geçersiz.")
        q = validate_quantity(quantity)
        if q <= 0:
            raise ValidationError("Miktar sıfırdan büyük olmalıdır.")
        with transaction(self.conn):
            line = self.conn.execute("SELECT COALESCE(MAX(line_no), 0) + 1 FROM purchase_items WHERE request_id = ?",
                                     (rid,)).fetchone()[0]
            return self.conn.execute(
                "INSERT INTO purchase_items(request_id, line_no, description, spec, unit, quantity, pos_kind,"
                " pos_ref_id, pos_code, pos_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, line, require(description, "Kalem tanımı"), clean(spec) or "", require(unit, "Birim"),
                 to_db_text(q, 3), pos.get("kind"), pos.get("ref_id"), clean(pos.get("code")) or "",
                 clean(pos.get("source")) or "")).lastrowid

    def duplicate_item(self, item_id: int) -> int:
        """Talep kaleminin kopyasını (bağlı poz dahil) talebin sonuna ekler; tekliflerdeki fiyatları kopyalanmaz."""
        r = self.conn.execute("SELECT request_id, description, unit, quantity, spec, pos_kind, pos_ref_id, pos_code,"
                              " pos_source FROM purchase_items WHERE id = ?", (item_id,)).fetchone()
        if r is None:
            raise NotFoundError("Talep kalemi bulunamadı.")
        pos = {"kind": r[5], "ref_id": r[6], "code": r[7], "source": r[8]} if r[5] else None
        return self.add_item(r[0], r[1], r[2], from_db_text(r[3]), r[4] or "", pos)

    def items(self, rid: int) -> list[RequestItem]:
        return [RequestItem(r[0], r[1], r[2], r[3], from_db_text(r[4]), r[5], r[6], r[7]) for r in self.conn.execute(
            "SELECT id, line_no, description, unit, quantity, spec, pos_code, pos_source FROM purchase_items"
            " WHERE request_id = ?"
            " ORDER BY line_no", (rid,))]

    # --- teklifler ------------------------------------------------------------
    def _offer_values(self, fields: dict) -> dict:
        v = {k: fields.get(k) for k in OFFER_FIELDS if k in fields}
        for k in MONEY_FIELDS:
            if k in v:
                v[k] = to_db_text(validate_price(v[k] or 0), 2)
        if "vat_rate" in v:
            v["vat_rate"] = to_db_text(_pct(v["vat_rate"], "KDV oranı"), 2)
        for k in ("offer_date", "valid_until"):
            if k in v:
                v[k] = validate_iso_date(v[k], "Teklif tarihi" if k == "offer_date" else "Geçerlilik tarihi")
        if "technical_ok" in v and v["technical_ok"] is not None:
            v["technical_ok"] = int(bool(v["technical_ok"]))
        for k in ("contact", "offer_no", "delivery_terms", "payment_terms", "warranty", "technical_note", "file_path"):
            if k in v:
                v[k] = clean(v[k]) or ""
        return v

    def add_offer(self, rid: int, firm: str, lines: list[dict] | None = None, **fields) -> int:
        self._editable(rid)
        v = self._offer_values(fields)
        cols = ["request_id", "firm"] + list(v)
        with transaction(self.conn):
            oid = self.conn.execute(f"INSERT INTO purchase_offers({', '.join(cols)}) VALUES "
                                    f"({', '.join('?' * len(cols))})",
                                    [rid, require(firm, "Firma adı")] + list(v.values())).lastrowid
            self._write_lines(rid, oid, lines or [])
            parties.link_unlinked(self.conn)
            audit.log(self.conn, "purchase_offer", oid, "EKLE", new={"firm": firm, "request": rid})
        return oid

    def _write_lines(self, rid: int, oid: int, lines: list[dict]) -> None:
        valid_items = {i.id: i for i in self.items(rid)}
        self.conn.execute("DELETE FROM purchase_offer_lines WHERE offer_id = ?", (oid,))
        for ln in lines:
            item = valid_items.get(ln["item_id"])
            if item is None:
                raise ValidationError("Teklif satırı talepte olmayan bir kaleme bağlanamaz.")
            q = validate_quantity(ln.get("quantity", item.quantity))
            self.conn.execute(
                "INSERT INTO purchase_offer_lines(offer_id, item_id, brand_model, quantity, unit, list_price,"
                " discount_rate, scope_ok, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, item.id, clean(ln.get("brand_model")) or "", to_db_text(q, 3),
                 clean(ln.get("unit")) or item.unit, to_db_text(validate_price(ln["list_price"]), 4),
                 to_db_text(_pct(ln.get("discount_rate", 0), "İskonto oranı"), 2), int(ln.get("scope_ok", True)),
                 clean(ln.get("note")) or ""))

    def set_offer_lines(self, oid: int, lines: list[dict]) -> None:
        rid = self._offer_row(oid)["request_id"]
        self._editable(rid)
        with transaction(self.conn):
            self._write_lines(rid, oid, lines)

    def _offer_row(self, oid: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM purchase_offers WHERE id = ?", (oid,)).fetchone()
        if r is None:
            raise NotFoundError("Teklif bulunamadı.")
        return r

    def revise_offer(self, oid: int, lines: list[dict] | None = None, **fields) -> int:
        """Yeni revizyon: eski teklif 'revize edildi' olur, değişmeden saklanır."""
        old = self._offer_row(oid)
        self._editable(old["request_id"])
        base = {k: old[k] for k in OFFER_FIELDS}
        base.update(fields)
        old_lines = [dict(item_id=r["item_id"], brand_model=r["brand_model"], quantity=from_db_text(r["quantity"]),
                          unit=r["unit"], list_price=from_db_text(r["list_price"]),
                          discount_rate=from_db_text(r["discount_rate"]), scope_ok=bool(r["scope_ok"]), note=r["note"])
                     for r in self.conn.execute("SELECT * FROM purchase_offer_lines WHERE offer_id = ?", (oid,))]
        for k in MONEY_FIELDS + ("vat_rate",):
            if isinstance(base.get(k), str):
                base[k] = from_db_text(base[k])
        v = self._offer_values(base)
        cols = ["request_id", "firm", "revision", "supersedes_id"] + list(v)
        with transaction(self.conn):
            new_id = self.conn.execute(
                f"INSERT INTO purchase_offers({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [old["request_id"], old["firm"], old["revision"] + 1, oid] + list(v.values())).lastrowid
            self._write_lines(old["request_id"], new_id, lines if lines is not None else old_lines)
            parties.link_unlinked(self.conn)
            self.conn.execute("UPDATE purchase_offers SET status = 'REVIZE_EDILDI' WHERE id = ?", (oid,))
            audit.log(self.conn, "purchase_offer", new_id, "EKLE", new={"revision_of": oid})
        return new_id

    def offers(self, rid: int, include_superseded: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM purchase_offers WHERE request_id = ?"
        if not include_superseded:
            sql += " AND status = 'GECERLI'"
        return list(self.conn.execute(sql + " ORDER BY firm, revision", (rid,)))

    def _offer(self, r: sqlite3.Row) -> Offer:
        lines = tuple(OfferLine(x["item_id"], from_db_text(x["quantity"]), x["unit"], from_db_text(x["list_price"]),
                                from_db_text(x["discount_rate"]), bool(x["scope_ok"]), x["brand_model"], x["note"])
                      for x in self.conn.execute("SELECT * FROM purchase_offer_lines WHERE offer_id = ?", (r["id"],)))
        return Offer(r["id"], f"{r['firm']} (rev. {r['revision']})", lines, from_db_text(r["vat_rate"]),
                     from_db_text(r["shipping"]), from_db_text(r["installation"]), from_db_text(r["other_costs"]),
                     r["offer_date"], r["valid_until"], r["delivery_days"], r["payment_terms"], r["warranty"],
                     None if r["technical_ok"] is None else bool(r["technical_ok"]))

    # --- değerlendirme ve karar ---------------------------------------------------
    def set_criteria(self, rid: int, criteria: list[tuple[str, Decimal]]) -> list[int]:
        self._editable(rid)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM purchase_criteria WHERE request_id = ?", (rid,))
            return [self.conn.execute("INSERT INTO purchase_criteria(request_id, name, weight) VALUES (?, ?, ?)",
                                      (rid, require(n, "Ölçüt adı"), to_db_text(validate_price(w), 2))).lastrowid
                    for n, w in criteria]

    def criteria(self, rid: int) -> list[tuple[int, str, Decimal]]:
        return [(r[0], r[1], from_db_text(r[2])) for r in self.conn.execute(
            "SELECT id, name, weight FROM purchase_criteria WHERE request_id = ? ORDER BY id", (rid,))]

    def set_score(self, criterion_id: int, offer_id: int, score, note: str = "") -> None:
        s = validate_price(score)
        if s > 10:
            raise ValidationError("Puan 0 ile 10 arasında olmalıdır.")
        with transaction(self.conn):
            self.conn.execute("INSERT INTO purchase_scores(criterion_id, offer_id, score, note) VALUES (?, ?, ?, ?)"
                              " ON CONFLICT(criterion_id, offer_id) DO UPDATE SET score = excluded.score,"
                              " note = excluded.note", (criterion_id, offer_id, to_db_text(s, 2), note))

    def scores(self, rid: int) -> dict[tuple[int, int], Decimal]:
        """(ölçüt, teklif) → puan."""
        return {(r[0], r[1]): from_db_text(r[2]) for r in self.conn.execute(
            "SELECT s.criterion_id, s.offer_id, s.score FROM purchase_scores s JOIN purchase_criteria c"
            " ON c.id = s.criterion_id WHERE c.request_id = ?", (rid,))}

    def evaluate(self, rid: int) -> Evaluation:
        req = self.get_request(rid)
        weights = {c: w for c, _, w in self.criteria(rid)}
        return evaluate(self.items(rid), [self._offer(r) for r in self.offers(rid)],
                        req.eval_date or date.today().isoformat(), weights, self.scores(rid))

    def decide(self, rid: int, offer_id: int, reason: str, evaluator: str, approver: str) -> None:
        authz.require("SATIN_ALMA", authz.APPROVE, "satın alma kararı")
        self._editable(rid)
        if self._offer_row(offer_id)["request_id"] != rid or self._offer_row(offer_id)["status"] != "GECERLI":
            raise ValidationError("Seçilen teklif bu talebin geçerli bir teklifi değil.")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE purchase_requests SET status = 'ONAYLANDI', selected_offer_id = ?, decision_reason = ?,"
                " evaluator = ?, approver = ?, decided_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')"
                " WHERE id = ?", (offer_id, require(reason, "Seçim gerekçesi"), require(evaluator, "Değerlendiren"),
                                  require(approver, "Onaylayan"), rid))
            audit.log(self.conn, "purchase_request", rid, "GUNCELLE",
                      new={"selected_offer": offer_id, "approver": approver}, note="Satın alma kararı")

    # --- onaylı kayıt: görüntüleme, geçmiş, kontrollü revizyon -------------------------
    def approved_detail(self, rid: int) -> dict:
        """Onaylı (karar verilmiş) talebin kalemleri, seçilen teklif, siparişi ve karar geçmişi (salt okunur)."""
        req = self.get_request(rid)
        if req.status != "ONAYLANDI":
            raise ConflictError("Yalnız onaylanmış (karar verilmiş) talebin ayrıntısı görüntülenir.")
        offer = self._offer_row(req.selected_offer_id)
        order = self.conn.execute("SELECT id, order_no, status FROM po_orders WHERE request_id = ?", (rid,)).fetchone()
        lines = {r["item_id"]: r for r in self.conn.execute(
            "SELECT * FROM purchase_offer_lines WHERE offer_id = ?", (offer["id"],))}
        items = []
        for it in self.items(rid):
            ln = lines.get(it.id)
            items.append({"item": it, "brand_model": ln["brand_model"] if ln else "",
                          "price": from_db_text(ln["list_price"]) * (1 - from_db_text(ln["discount_rate"]) / 100)
                          if ln else None})
        return {"request": req, "offer": offer, "items": items, "order": order, "history": self.decision_history(rid)}

    def decision_history(self, rid: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT at, action, old_json, new_json, note FROM audit_log WHERE entity = 'purchase_request' AND"
            " entity_id = ? ORDER BY id", (rid,)).fetchall()

    def reopen_for_revision(self, rid: int, reason: str, user: str) -> None:
        """Onaylı talebi kontrollü olarak revizyona açar: karar kaldırılır (talep TASLAK olur), önceki karar ve
        gerekçesi değişiklik kaydına yazılır. Siparişi açılmış talep açılamaz; önce sipariş iptal edilmelidir."""
        req = self.get_request(rid)
        if req.status != "ONAYLANDI":
            raise ConflictError("Yalnız onaylanmış talep revizyona açılabilir.")
        if self.conn.execute("SELECT 1 FROM po_orders WHERE request_id = ? AND status <> 'IPTAL'", (rid,)).fetchone():
            raise ConflictError("Bu talep için sipariş açılmış; önce siparişi iptal edin (teslimi olan sipariş "
                                "iptal edilemez).")
        if self.conn.execute("SELECT 1 FROM po_orders WHERE request_id = ?", (rid,)).fetchone():
            raise ConflictError("İptal edilmiş sipariş kaydı bulunuyor; kayıt bütünlüğü için talep yeniden açılamaz. "
                                "Yeni bir talep açın.")
        reason, user = require(reason, "Revizyon gerekçesi"), require(user, "İşlemi yapan")
        with transaction(self.conn):
            self.conn.execute("UPDATE purchase_requests SET status = 'TASLAK', selected_offer_id = NULL,"
                              " decision_reason = NULL, evaluator = NULL, approver = NULL, decided_at = NULL"
                              " WHERE id = ?", (rid,))
            audit.log(self.conn, "purchase_request", rid, "GUNCELLE",
                      old={"status": "ONAYLANDI", "selected_offer": req.selected_offer_id,
                           "decision_reason": req.decision_reason, "approver": req.approver},
                      new={"status": "TASLAK", "revised_by": user}, note=f"Revizyona açıldı: {reason}")

    def custom_price_from_offer(self, offer_id: int, item_id: int, custom_service) -> int:
        """Teklif satırından kaynağı belirtilmiş özel poz oluşturur (kütüphaneye yazmaz)."""
        r = self._offer_row(offer_id)
        ln = next((x for x in self._offer(r).lines if x.item_id == item_id), None)
        item = next((i for i in self.items(r["request_id"]) if i.id == item_id), None)
        if ln is None or item is None:
            raise NotFoundError("Teklif satırı bulunamadı.")
        basis = (f"Satın alma teklifi: {r['firm']} (rev. {r['revision']}), teklif no {r['offer_no'] or '-'}, tarih "
                 f"{r['offer_date'] or '-'}; talep #{r['request_id']}. Net birim fiyat (iskonto sonrası, KDV hariç).")
        return custom_service.create(item.description, item.unit, item.spec or item.description,
                                     ln.net_unit_price.quantize(Decimal("0.01")),
                                     r["offer_date"] or date.today().isoformat(), basis, category="Satın alma teklifi")
