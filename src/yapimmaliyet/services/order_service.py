"""Satın alma siparişi ve teslim: onaylanmış satın alma kararından sipariş, irsaliyeli teslim kayıtları ve ödeme.

Kurallar:
- Sipariş yalnız onaylanmış (karar verilmiş) talepten ve seçilen tekliften açılır; talep başına tek sipariş.
  Satır miktarı talep miktarı, birim fiyat seçilen teklifin net birim fiyatıdır (iskonto sonrası, KDV hariç,
  4 basamak); teklifte olmayan kalem siparişe girmez ve bildirilir. Nakliye + montaj + diğer giderler ek gider olur.
- Teslim edilen kümülatif miktar sipariş miktarını aşamaz; iptal edilmiş siparişe teslim girilmez; teslimi olan
  sipariş iptal edilemez; ödendi işaretli teslim silinemez.
- Teslim tutarı = Σ round(miktar × birim fiyat, 2) + ek gider payı. Ek gider teslim edilen mal tutarı oranında
  dağıtılır (kümülatif payın yuvarlanmış farkı; sipariş tamamlanınca toplam ek gidere eşit olur).
- Gerçekleşen maliyet teslim tarihine, nakit çıkışı ödeme tarihine göre bu kayıtlardan okunur (kopyalanmaz).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services import attachment_store
from yapimmaliyet.services.validators import clean, require, validate_iso_date

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class OrderLine:
    id: int
    line_no: int
    description: str
    unit: str
    quantity: Decimal
    unit_price: Decimal
    delivered: Decimal

    @property
    def amount(self) -> Decimal:
        return quantize(self.quantity * self.unit_price, 2)

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.delivered


@dataclass(frozen=True)
class Delivery:
    id: int
    delivery_date: str
    waybill_no: str | None
    received_by: str | None
    paid_date: str | None
    notes: str | None
    quantities: dict[int, Decimal]   # sipariş satırı → miktar
    goods: Decimal                   # Σ round(miktar × fiyat, 2)
    extra: Decimal                   # ek gider payı
    vat: Decimal
    waybill_file: str | None = None  # irsaliyenin orijinal taraması (veri klasörüne göreli yol)

    @property
    def amount(self) -> Decimal:
        return self.goods + self.extra


@dataclass
class Order:
    id: int
    request_id: int
    offer_id: int
    project_id: int | None
    order_no: str
    order_date: str
    supplier: str
    due_date: str | None
    vat_rate: Decimal                # yüzde
    extra_costs: Decimal
    cancelled: bool
    notes: str | None
    lines: list[OrderLine] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)

    @property
    def goods_total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines), ZERO)

    @property
    def total_excl_vat(self) -> Decimal:
        return self.goods_total + self.extra_costs

    @property
    def vat(self) -> Decimal:
        return quantize(self.total_excl_vat * self.vat_rate / HUNDRED, 2)

    @property
    def delivered_amount(self) -> Decimal:
        return sum((d.amount for d in self.deliveries), ZERO)

    @property
    def complete(self) -> bool:
        return bool(self.lines) and all(ln.remaining <= 0 for ln in self.lines)

    @property
    def status(self) -> str:
        if self.cancelled:
            return "İptal"
        if self.complete:
            return "Tamamlandı"
        return "Kısmi teslim" if self.deliveries else "Teslim bekliyor"

    def late(self, today: date | None = None) -> bool:
        return (not self.cancelled and not self.complete and self.due_date is not None
                and self.due_date < (today or date.today()).isoformat())


def _qty(value, label: str) -> Decimal:
    try:
        d = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} geçerli bir sayı değil: {value}") from exc
    if d < 0:
        raise ValidationError(f"{label} negatif olamaz.")
    return d


class OrderService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path | None = None):
        self.conn = conn
        self.data_dir = Path(data_dir) if data_dir else None

    # --- sipariş --------------------------------------------------------------
    def open_requests(self, project_id: int | None = None) -> list[sqlite3.Row]:
        """Karar verilmiş, henüz siparişe dönüştürülmemiş talepler (proje verilirse o proje ve projesizler)."""
        rows = self.conn.execute(
            "SELECT r.id, r.title, r.project_id, o.firm, o.revision FROM purchase_requests r"
            " JOIN purchase_offers o ON o.id = r.selected_offer_id"
            " WHERE r.status = 'ONAYLANDI' AND NOT EXISTS (SELECT 1 FROM po_orders p WHERE p.request_id = r.id)"
            " ORDER BY r.id").fetchall()
        return [r for r in rows if project_id is None or r["project_id"] in (None, project_id)]

    def create_from_request(self, request_id: int, order_no: str, order_date: str, due_date: str | None = None,
                            notes: str | None = None) -> tuple[int, list[str]]:
        """Siparişi açar; (sipariş kimliği, teklifte bulunmadığı için siparişe girmeyen kalemler) döndürür."""
        req = self.conn.execute("SELECT * FROM purchase_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None:
            raise NotFoundError("Satın alma talebi bulunamadı.")
        if req["status"] != "ONAYLANDI" or req["selected_offer_id"] is None:
            raise ValidationError("Sipariş yalnız karar verilmiş (onaylanmış) satın alma talebinden açılır.")
        if self.conn.execute("SELECT 1 FROM po_orders WHERE request_id = ?", (request_id,)).fetchone():
            raise ConflictError("Bu talep için sipariş zaten açılmış.")
        offer = self.conn.execute("SELECT * FROM purchase_offers WHERE id = ?", (req["selected_offer_id"],)).fetchone()
        od = validate_iso_date(order_date, "Sipariş tarihi", required=True)
        dd = validate_iso_date(due_date, "Termin tarihi")
        if dd is None and offer["delivery_days"] is not None:
            dd = (date.fromisoformat(od) + timedelta(days=offer["delivery_days"])).isoformat()
        if dd is not None and dd < od:
            raise ValidationError("Termin tarihi sipariş tarihinden önce olamaz.")
        offered = {r["item_id"]: r for r in self.conn.execute(
            "SELECT * FROM purchase_offer_lines WHERE offer_id = ?", (offer["id"],))}
        items = self.conn.execute("SELECT * FROM purchase_items WHERE request_id = ? ORDER BY line_no",
                                  (request_id,)).fetchall()
        missing = [it["description"] for it in items if it["id"] not in offered]
        if len(missing) == len(items):
            raise ValidationError("Seçilen teklifte talep kalemlerinin hiçbiri yok; sipariş açılamaz.")
        extra = sum((from_db_text(offer[k]) for k in ("shipping", "installation", "other_costs")), ZERO)
        with transaction(self.conn):
            oid = self.conn.execute(
                "INSERT INTO po_orders(request_id, offer_id, project_id, order_no, order_date, supplier, due_date,"
                " vat_rate, extra_costs, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, offer["id"], req["project_id"], require(order_no, "Sipariş no"), od, offer["firm"], dd,
                 offer["vat_rate"], to_db_text(extra, 2), clean(notes))).lastrowid
            n = 0
            for it in items:
                ol = offered.get(it["id"])
                if ol is None:
                    continue
                n += 1
                price = from_db_text(ol["list_price"]) * (1 - from_db_text(ol["discount_rate"]) / HUNDRED)
                self.conn.execute(
                    "INSERT INTO po_order_lines(order_id, item_id, line_no, description, unit, quantity, unit_price)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)", (oid, it["id"], n, it["description"], it["unit"],
                                                      it["quantity"], to_db_text(quantize(price, 4), 4)))
            audit.log(self.conn, "po_order", oid, "EKLE", new={"request": request_id, "offer": offer["id"],
                                                               "order_no": order_no})
        return oid, missing

    def create_direct(self, supplier: str, lines: list[dict], order_no: str, order_date: str,
                      project_id: int | None = None, due_date: str | None = None, vat_rate="20",
                      extra_costs="0", notes: str | None = None, title: str | None = None) -> int:
        """Karşılaştırma yapılmadan tek tedarikçiden doğrudan sipariş (manuel kalemlerle).

        Satın alma kaydının bütünlüğü için arkada tek teklifli, karar verilmiş bir talep açılır (gerekçe:
        "Doğrudan sipariş"); sipariş, teslim ve maliyet bağlantıları karardan açılan siparişle aynıdır.
        `lines`: [{description, unit, quantity, unit_price}].
        """
        from yapimmaliyet.services.purchase_service import PurchaseService

        supplier = require(supplier, "Tedarikçi")
        order_no = require(order_no, "Sipariş no")
        if not lines:
            raise ValidationError("Siparişe en az bir kalem ekleyin.")
        if self.conn.execute("SELECT 1 FROM po_orders WHERE order_no = ? AND supplier = ?", (order_no, supplier)
                             ).fetchone():
            raise ConflictError("Bu tedarikçi için aynı sipariş numarası zaten kayıtlı.")
        od = validate_iso_date(order_date, "Sipariş tarihi", required=True)
        dd = validate_iso_date(due_date, "Termin tarihi")
        if dd is not None and dd < od:
            raise ValidationError("Termin tarihi sipariş tarihinden önce olamaz.")
        ps = PurchaseService(self.conn)
        rid = ps.create_request(clean(title) or f"Doğrudan sipariş {order_no}",
                                "Doğrudan sipariş (manuel kalemler)", project_id)
        try:   # adımlar ayrı transaction'dır; hata olursa açılan talep (kalem ve teklifiyle) geri silinir
            offer_lines = []
            for ln in lines:
                item = ps.add_item(rid, ln["description"], ln["unit"], ln["quantity"])
                offer_lines.append({"item_id": item, "list_price": ln["unit_price"], "discount_rate": 0})
            offer = ps.add_offer(rid, supplier, offer_lines, offer_date=od, vat_rate=vat_rate,
                                 other_costs=extra_costs or 0, offer_no=order_no)
            ps.decide(rid, offer, "Doğrudan sipariş (karşılaştırmasız, tek tedarikçi)", "Doğrudan sipariş",
                      "Doğrudan sipariş")
            return self.create_from_request(rid, order_no, od, dd, notes)[0]
        except BaseException:
            with transaction(self.conn):
                self.conn.execute("DELETE FROM purchase_requests WHERE id = ?", (rid,))
            raise

    def cancel(self, order_id: int, reason: str) -> None:
        o = self.order(order_id)
        if o.deliveries:
            raise ConflictError("Teslim kaydı olan sipariş iptal edilemez.")
        if o.cancelled:
            raise ConflictError("Sipariş zaten iptal edilmiş.")
        with transaction(self.conn):
            self.conn.execute("UPDATE po_orders SET status = 'IPTAL', notes = trim(COALESCE(notes, '') || ' ' || ?)"
                              " WHERE id = ?", (f"İptal: {require(reason, 'İptal gerekçesi')}", order_id))
            audit.log(self.conn, "po_order", order_id, "GUNCELLE", new={"status": "IPTAL"}, note=reason)

    def orders(self, project_id: int | None = None) -> list[Order]:
        ids = [r[0] for r in self.conn.execute("SELECT id, project_id FROM po_orders ORDER BY id")
               if project_id is None or r[1] in (None, project_id)]
        return [self.order(i) for i in ids]

    def order(self, order_id: int) -> Order:
        r = self.conn.execute("SELECT * FROM po_orders WHERE id = ?", (order_id,)).fetchone()
        if r is None:
            raise NotFoundError("Sipariş bulunamadı.")
        dels = self.conn.execute("SELECT * FROM po_deliveries WHERE order_id = ? ORDER BY delivery_date, id",
                                 (order_id,)).fetchall()
        dq: dict[int, dict[int, Decimal]] = {d["id"]: {} for d in dels}
        for x in self.conn.execute("SELECT l.* FROM po_delivery_lines l JOIN po_deliveries d ON d.id = l.delivery_id"
                                   " WHERE d.order_id = ?", (order_id,)):
            dq[x["delivery_id"]][x["order_line_id"]] = from_db_text(x["quantity"])
        delivered: dict[int, Decimal] = {}
        for q in dq.values():
            for lid, v in q.items():
                delivered[lid] = delivered.get(lid, ZERO) + v
        lines = [OrderLine(x["id"], x["line_no"], x["description"], x["unit"], from_db_text(x["quantity"]),
                           from_db_text(x["unit_price"]), delivered.get(x["id"], ZERO)) for x in self.conn.execute(
            "SELECT * FROM po_order_lines WHERE order_id = ? ORDER BY line_no", (order_id,))]
        o = Order(r["id"], r["request_id"], r["offer_id"], r["project_id"], r["order_no"], r["order_date"],
                  r["supplier"], r["due_date"], from_db_text(r["vat_rate"]), from_db_text(r["extra_costs"]),
                  r["status"] == "IPTAL", r["notes"], lines)
        price = {ln.id: ln.unit_price for ln in lines}
        goods_order, goods_cum, share_cum = o.goods_total, ZERO, ZERO
        for d in dels:
            q = dq[d["id"]]
            goods = sum((quantize(v * price[lid], 2) for lid, v in q.items()), ZERO)
            goods_cum += goods
            new_cum = quantize(o.extra_costs * goods_cum / goods_order, 2) if goods_order else ZERO
            extra, share_cum = new_cum - share_cum, new_cum
            o.deliveries.append(Delivery(d["id"], d["delivery_date"], d["waybill_no"], d["received_by"],
                                         d["paid_date"], d["notes"], q, goods, extra,
                                         quantize((goods + extra) * o.vat_rate / HUNDRED, 2),
                                         d["waybill_file"]))
        return o

    # --- teslim ---------------------------------------------------------------
    def add_delivery(self, order_id: int, delivery_date: str, quantities: dict[int, object],
                     waybill_no: str | None = None, received_by: str | None = None, notes: str | None = None,
                     waybill_file: str | Path | None = None) -> int:
        o = self.order(order_id)
        if o.cancelled:
            raise ConflictError("İptal edilmiş siparişe teslim girilemez.")
        dd = validate_iso_date(delivery_date, "Teslim tarihi", required=True)
        if dd < o.order_date:
            raise ValidationError("Teslim tarihi sipariş tarihinden önce olamaz.")
        by_id = {ln.id: ln for ln in o.lines}
        qs = {}
        for lid, raw in quantities.items():
            if lid not in by_id:
                raise ValidationError("Teslim satırı bu siparişe ait değil.")
            q = _qty(raw, f"{by_id[lid].description} teslim miktarı")
            if q > by_id[lid].remaining:
                raise ValidationError(f"{by_id[lid].description}: teslim miktarı ({q}) kalan sipariş miktarını "
                                      f"({by_id[lid].remaining}) aşıyor.")
            if q:
                qs[lid] = q
        if not qs:
            raise ValidationError("En az bir satırda teslim miktarı girilmelidir.")
        stored = attachment_store.store(self.data_dir, "Irsaliyeler", waybill_file) if waybill_file else None
        with transaction(self.conn):
            did = self.conn.execute(
                "INSERT INTO po_deliveries(order_id, delivery_date, waybill_no, received_by, notes, waybill_file)"
                " VALUES (?, ?, ?, ?, ?, ?)", (order_id, dd, clean(waybill_no), clean(received_by), clean(notes),
                                               stored)).lastrowid
            for lid, q in qs.items():
                self.conn.execute("INSERT INTO po_delivery_lines(delivery_id, order_line_id, quantity) VALUES (?, ?, ?)",
                                  (did, lid, to_db_text(q, 3)))
            audit.log(self.conn, "po_delivery", did, "EKLE",
                      new={"order": order_id, "waybill": waybill_no, "lines": {str(k): str(v) for k, v in qs.items()}})
        return did

    def set_waybill_file(self, delivery_id: int, src: str | Path | None) -> None:
        """İrsaliyenin orijinal taramasını teslime bağlar (None: bağlantıyı kaldırır; kopya silinmez)."""
        self._delivery_row(delivery_id)
        stored = attachment_store.store(self.data_dir, "Irsaliyeler", src) if src else None
        with transaction(self.conn):
            self.conn.execute("UPDATE po_deliveries SET waybill_file = ? WHERE id = ?", (stored, delivery_id))
            audit.log(self.conn, "po_delivery", delivery_id, "GUNCELLE", new={"waybill_file": stored})

    def waybill_path(self, delivery_id: int) -> Path | None:
        return attachment_store.resolve(self.data_dir, self._delivery_row(delivery_id)["waybill_file"])

    def _delivery_row(self, delivery_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM po_deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if r is None:
            raise NotFoundError("Teslim kaydı bulunamadı.")
        return r

    def mark_paid(self, delivery_id: int, paid_date: str | None) -> None:
        """Ödeme tarihini yazar (boş: ödenmedi)."""
        r = self._delivery_row(delivery_id)
        pd = validate_iso_date(paid_date, "Ödeme tarihi")
        if pd is not None and pd < r["delivery_date"]:
            raise ValidationError("Ödeme tarihi teslim tarihinden önce olamaz.")
        with transaction(self.conn):
            self.conn.execute("UPDATE po_deliveries SET paid_date = ? WHERE id = ?", (pd, delivery_id))
            audit.log(self.conn, "po_delivery", delivery_id, "GUNCELLE", old={"paid_date": r["paid_date"]},
                      new={"paid_date": pd})

    def delete_delivery(self, delivery_id: int) -> None:
        r = self._delivery_row(delivery_id)
        if r["paid_date"]:
            raise ConflictError("Ödendi işaretli teslim silinemez; önce ödeme tarihini kaldırın.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM po_deliveries WHERE id = ?", (delivery_id,))
            audit.log(self.conn, "po_delivery", delivery_id, "SIL", old={"order": r["order_id"]})

    def project_deliveries(self, project_id: int) -> list[tuple[Order, Delivery]]:
        """Projenin (iptal edilmemiş) siparişlerindeki teslimler — gerçekleşen maliyet ve nakit akışı için."""
        return [(o, d) for o in self.orders() if o.project_id == project_id and not o.cancelled
                for d in o.deliveries]
