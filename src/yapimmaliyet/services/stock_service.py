"""Stok ve depo: teslimlerden gelen stok girişi ile sarf / çıkış kayıtları (miktar takibi).

Faz 1 (docs/STOK_GECIS_TASARIMI.md): depo/saha kartları, depoya iade, transfer ve sayım düzeltmesi — hepsi YALNIZ MİKTARDIR;
maliyet, taahhüt ve EAC değişmez ("teslim = maliyet" politikası aynen sürer). Depo bakiyesi: varsayılan depo = toplam − adlandırılmış
depolar; hiçbir bakiye negatif olamaz. Hareketler değiştirilmez/silinmez.

Stok girişi ayrı girilmez: sipariş teslim kayıtları (irsaliyeli) otomatik girişdir. Çıkış kayıtları sipariş satırına bağlı
girilir; kalan stoktan fazla çıkış yapılamaz. Çıkış, isteğe bağlı olarak faaliyete atanır. Maliyet teslimde tanınır; stok
değeri (kalan × net birim fiyat) yalnız bilgi amaçlıdır ve gerçekleşen maliyeti değiştirmez.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import from_db_text, to_db_text, to_decimal
from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.infra.errors import NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, validate_iso_date

ZERO = Decimal(0)


@dataclass(frozen=True)
class StockRow:
    line_id: int
    project_id: int | None
    order_no: str
    supplier: str
    description: str
    unit: str
    received: Decimal
    issued: Decimal
    unit_price: Decimal
    last_move: str | None
    returned: Decimal = ZERO       # depoya iade (çıkışın geri dönüşü)
    adjusted: Decimal = ZERO       # sayım düzeltmeleri toplamı (işaretli)

    @property
    def balance(self) -> Decimal:
        return self.received - self.issued + self.returned + self.adjusted

    @property
    def value(self) -> Decimal:
        """Kalan stoğun net birim fiyatla değeri (bilgi)."""
        return quantize(self.balance * self.unit_price, 2)


@dataclass(frozen=True)
class Movement:
    date: str
    kind: str                      # GIRIS | CIKIS | IADE_DEPO | TRANSFER | SAYIM
    line_id: int
    order_no: str
    description: str
    unit: str
    quantity: Decimal              # işaretsiz miktar (sayımda işaretli)
    effect: Decimal                # toplam stoğa etkisi (transfer 0)
    from_name: str
    to_name: str
    doc_no: str
    note: str
    delivery_id: int | None = None   # GIRIS: irsaliyeli teslim kaydı (irsaliye dosyasına ulaşmak için)


MOVE_LABELS = {"GIRIS": "Teslim girişi", "CIKIS": "Çıkış / sarf", "IADE_DEPO": "Depoya iade", "TRANSFER": "Transfer", "SAYIM": "Sayım düzeltmesi"}
DEFAULT_DEPOT = "Varsayılan depo"


@dataclass(frozen=True)
class Issue:
    id: int
    line_id: int
    issue_date: str
    quantity: Decimal
    location: str
    note: str
    activity_id: int | None


class StockService:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path | None = None):
        self.conn = conn
        self.data_dir = Path(data_dir) if data_dir else None

    def receive(self, receipt_date: str, lines: list[dict], supplier: str = "", waybill_no: str = "",
                project_id: int | None = None, location_id: int | None = None, waybill_file=None,
                received_by: str = "", note: str = "") -> tuple[int, list[int]]:
        """Elle / Excel'den stok girişi: irsaliyeli bir teslim kaydıdır. `lines`: [{description, unit, quantity, code?}].

        YALNIZ MİKTAR: giriş, birim fiyatı 0 olan bir stok giriş siparişinin irsaliyeli teslimi olarak açılır; maliyet,
        taahhüt ve EAC üretmez ("teslim = maliyet" politikası değişmez; satın alınan malın maliyeti siparişin kendi
        teslimiyle tanınır). Hareket defterinde irsaliye numarası görünür; `waybill_file` irsaliyenin orijinal taramasıdır.
        Döndürür: (sipariş kimliği, stok kalemi / sipariş satırı kimlikleri)."""
        from yapimmaliyet.services.order_service import OrderService

        if not lines:
            raise ValidationError("Stok girişi için en az bir kalem girin.")
        rd = validate_iso_date(receipt_date, "Giriş tarihi", required=True)
        who = clean(supplier) or "Stok girişi"
        orders = OrderService(self.conn, self.data_dir)
        base = f"STOK-{clean(waybill_no) or rd}"
        order_no, n = base, 1
        while self.conn.execute("SELECT 1 FROM po_orders WHERE order_no = ? AND supplier = ?", (order_no, who)).fetchone():
            n += 1
            order_no = f"{base}-{n}"
        items = []
        for ln in lines:
            desc = clean(ln.get("description"))
            if not desc or not clean(ln.get("unit")):
                raise ValidationError("Her stok kaleminin adı ve birimi olmalıdır.")
            code = clean(ln.get("code"))
            items.append({"description": f"[{code}] {desc}" if code else desc, "unit": clean(ln["unit"]),
                          "quantity": ln["quantity"], "unit_price": 0})
        oid = orders.create_direct(who, items, order_no, rd, project_id, vat_rate="0", extra_costs="0",
                                   notes=clean(note) or "Stok girişi (yalnız miktar; maliyet üretmez)",
                                   title=f"Stok girişi {order_no}")
        order = orders.order(oid)
        orders.add_delivery(oid, rd, {ln.id: ln.quantity for ln in order.lines}, clean(waybill_no), clean(received_by),
                            clean(note), waybill_file)
        line_ids = [ln.id for ln in order.lines]
        if location_id is not None:
            for ln in order.lines:
                self.transfer(ln.id, rd, ln.quantity, None, location_id, reason="Giriş deposu")
        return oid, line_ids

    def balances(self, project_id: int | None = None, only_positive: bool = False) -> list[StockRow]:
        rows = self.conn.execute(
            "SELECT l.id, o.project_id, o.order_no, o.supplier, l.description, l.unit, l.unit_price,"
            " (SELECT MAX(d.delivery_date) FROM po_delivery_lines dl JOIN po_deliveries d ON d.id = dl.delivery_id"
            "  WHERE dl.order_line_id = l.id) AS last_in,"
            " (SELECT MAX(s.issue_date) FROM stk_issues s WHERE s.order_line_id = l.id) AS last_out,"
            " (SELECT MAX(m.move_date) FROM stk_moves m WHERE m.order_line_id = l.id) AS last_mv"
            " FROM po_order_lines l JOIN po_orders o ON o.id = l.order_id WHERE o.status <> 'IPTAL'"
            " AND (? IS NULL OR o.project_id = ?) ORDER BY o.order_no, l.line_no", (project_id, project_id)).fetchall()
        out = []
        for r in rows:
            received = self._received(r["id"])
            if received == 0:
                continue
            issued = sum((from_db_text(x["quantity"]) for x in self.conn.execute(
                "SELECT quantity FROM stk_issues WHERE order_line_id = ?", (r["id"],))), ZERO)
            returned, adjusted = self._move_totals(r["id"])
            row = StockRow(r["id"], r["project_id"], r["order_no"], r["supplier"], r["description"], r["unit"], received,
                           issued, from_db_text(r["unit_price"]),
                           max(filter(None, (r["last_in"], r["last_out"], r["last_mv"])), default=None), returned, adjusted)
            if not only_positive or row.balance > 0:
                out.append(row)
        return out

    def _move_totals(self, line_id: int) -> tuple[Decimal, Decimal]:
        ret = adj = ZERO
        for m in self.conn.execute("SELECT kind, quantity FROM stk_moves WHERE order_line_id = ?", (line_id,)):
            if m["kind"] == "IADE_DEPO":
                ret += from_db_text(m["quantity"])
            elif m["kind"] == "SAYIM":
                adj += from_db_text(m["quantity"])
        return ret, adj

    def _received(self, line_id: int) -> Decimal:
        return sum((from_db_text(x["quantity"]) for x in self.conn.execute(
            "SELECT quantity FROM po_delivery_lines WHERE order_line_id = ?", (line_id,))), ZERO)

    def row(self, line_id: int) -> StockRow:
        r = next((x for x in self.balances() if x.line_id == line_id), None)
        if r is None:
            raise NotFoundError("Stokta bu kalem yok (teslim edilmemiş ya da sipariş iptal).")
        return r

    def issues(self, line_id: int) -> list[Issue]:
        return [Issue(r["id"], r["order_line_id"], r["issue_date"], from_db_text(r["quantity"]), r["location"], r["note"],
                      r["activity_id"]) for r in self.conn.execute(
            "SELECT * FROM stk_issues WHERE order_line_id = ? ORDER BY issue_date, id", (line_id,))]

    def issue(self, line_id: int, issue_date: str, quantity, location: str = "", note: str = "",
              activity_id: int | None = None, location_id: int | None = None) -> int:
        row = self.row(line_id)
        if location_id is not None:
            self._check_location(location_id, row)
        try:
            q = to_decimal(quantity)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Çıkış miktarı geçerli bir sayı değil.") from exc
        if q <= 0:
            raise ValidationError("Çıkış miktarı sıfırdan büyük olmalıdır.")
        if q > row.balance:
            raise ValidationError(f"Çıkış miktarı ({q}) kalan stoktan ({row.balance}) fazla olamaz.")
        d = validate_iso_date(issue_date, "Çıkış tarihi", required=True)
        first = self.conn.execute("SELECT MIN(d.delivery_date) FROM po_delivery_lines dl JOIN po_deliveries d ON d.id = "
                                  "dl.delivery_id WHERE dl.order_line_id = ?", (line_id,)).fetchone()[0]
        if first and d < first:
            raise ValidationError("Çıkış tarihi ilk teslim tarihinden önce olamaz.")
        if activity_id is not None and self.conn.execute(
                "SELECT 1 FROM ws_activities WHERE id = ? AND project_id IS ?", (activity_id, row.project_id)).fetchone() is None:
            raise ValidationError("Seçilen faaliyet siparişin projesine ait değil.")
        with transaction(self.conn):
            iid = self.conn.execute(
                "INSERT INTO stk_issues(order_line_id, project_id, activity_id, issue_date, quantity, location, note, location_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (line_id, row.project_id, activity_id, d, to_db_text(q, 3),
                                                     clean(location) or (self._location_name(location_id) if location_id else ""),
                                                     clean(note) or "", location_id)).lastrowid
            self._validate(line_id)
            audit.log(self.conn, "stk_issue", iid, "EKLE", new={"line": line_id, "qty": str(q), "item": row.description})
        return iid

    def delete_issue(self, issue_id: int) -> None:
        r = self.conn.execute("SELECT * FROM stk_issues WHERE id = ?", (issue_id,)).fetchone()
        if r is None:
            raise NotFoundError("Çıkış kaydı bulunamadı.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM stk_issues WHERE id = ?", (issue_id,))
            self._validate(r["order_line_id"], "Bu çıkış, sonradan yapılan iade/transfer/sayım hareketlerine dayanıyor; silinirse bakiye tutarsız olur.")
            audit.log(self.conn, "stk_issue", issue_id, "SIL", old={"qty": r["quantity"]})

    # --- depo / saha kartları -------------------------------------------------------
    def _location_name(self, location_id: int) -> str:
        r = self.conn.execute("SELECT name FROM stk_locations WHERE id = ?", (location_id,)).fetchone()
        return r["name"] if r else ""

    def create_location(self, name: str, project_id: int | None = None, note: str = "") -> int:
        name = clean(name)
        if not name:
            raise ValidationError("Depo adı boş bırakılamaz.")
        if name.casefold() == DEFAULT_DEPOT.casefold():
            raise ValidationError("Bu ad varsayılan depo için ayrılmıştır.")
        if project_id is not None and self.conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
            raise NotFoundError("Proje bulunamadı.")
        if self.conn.execute("SELECT 1 FROM stk_locations WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            raise ValidationError("Bu adla bir depo zaten var.")
        with transaction(self.conn):
            lid = self.conn.execute("INSERT INTO stk_locations(name, project_id, note) VALUES (?, ?, ?)",
                                    (name, project_id, clean(note) or "")).lastrowid
            audit.log(self.conn, "stk_location", lid, "EKLE", new={"name": name, "project": project_id})
        return lid

    def locations(self, project_id: int | None = None, include_inactive: bool = False) -> list[sqlite3.Row]:
        """Proje verilirse o projeye ait ve genel (projesiz) depolar."""
        sql = "SELECT * FROM stk_locations WHERE (? IS NULL OR project_id IS NULL OR project_id = ?)"
        if not include_inactive:
            sql += " AND active = 1"
        return self.conn.execute(sql + " ORDER BY name COLLATE NOCASE", (project_id, project_id)).fetchall()

    def set_location_active(self, location_id: int, active: bool) -> None:
        if self.conn.execute("SELECT 1 FROM stk_locations WHERE id = ?", (location_id,)).fetchone() is None:
            raise NotFoundError("Depo bulunamadı.")
        if not active:
            for r in self.conn.execute("SELECT DISTINCT order_line_id FROM stk_moves WHERE from_location_id = ? OR to_location_id = ?"
                                       " UNION SELECT DISTINCT order_line_id FROM stk_issues WHERE location_id = ?",
                                       (location_id, location_id, location_id)).fetchall():
                if self.location_balances(r[0]).get(location_id, ZERO) != 0:
                    raise ValidationError("Depoda stok bulunuyor; önce transfer edin ya da sayım düzeltmesi yapın.")
        with transaction(self.conn):
            self.conn.execute("UPDATE stk_locations SET active = ? WHERE id = ?", (int(active), location_id))
            audit.log(self.conn, "stk_location", location_id, "GUNCELLE", new={"active": active})

    def _check_location(self, location_id: int, row: StockRow) -> None:
        loc = self.conn.execute("SELECT * FROM stk_locations WHERE id = ?", (location_id,)).fetchone()
        if loc is None:
            raise NotFoundError("Depo bulunamadı.")
        if not loc["active"]:
            raise ValidationError(f"“{loc['name']}” deposu pasif.")
        if loc["project_id"] is not None and loc["project_id"] != row.project_id:
            raise ValidationError(f"“{loc['name']}” deposu başka bir projeye ait; proje dışı stok hareketi bu aşamada yapılamaz "
                                  "(maliyet projeye bağlı kalır).")

    def location_balances(self, line_id: int) -> dict[int | None, Decimal]:
        """Kalem için depo bakiyeleri; anahtar None = varsayılan depo (toplam − adlandırılmış depolar)."""
        named: dict[int, Decimal] = {}

        def add(loc, q):
            if loc is not None:
                named[loc] = named.get(loc, ZERO) + q

        for m in self.conn.execute("SELECT * FROM stk_moves WHERE order_line_id = ?", (line_id,)):
            q = from_db_text(m["quantity"])
            if m["kind"] == "TRANSFER":
                add(m["from_location_id"], -q)
                add(m["to_location_id"], q)
            else:                                   # IADE_DEPO (+), SAYIM (işaretli)
                add(m["to_location_id"], q)
        for i in self.conn.execute("SELECT quantity, location_id FROM stk_issues WHERE order_line_id = ?", (line_id,)):
            add(i["location_id"], -from_db_text(i["quantity"]))
        received = self._received(line_id)
        issued = sum((from_db_text(x["quantity"]) for x in self.conn.execute(
            "SELECT quantity FROM stk_issues WHERE order_line_id = ?", (line_id,))), ZERO)
        returned, adjusted = self._move_totals(line_id)
        total = received - issued + returned + adjusted
        out: dict[int | None, Decimal] = {None: total - sum(named.values(), ZERO)}
        out.update(named)
        return out

    def _validate(self, line_id: int, msg: str = "") -> None:
        """Hareketten sonra toplam, depo ve iade bakiyeleri negatif olamaz; aksi hâlde işlem geri alınır."""
        bad = [k for k, v in self.location_balances(line_id).items() if v < 0]
        if bad:
            names = ", ".join(DEFAULT_DEPOT if k is None else self._location_name(k) for k in bad)
            raise ValidationError(msg or f"Hareket sonrası bakiye negatif olur ({names}); miktarı ya da depoyu kontrol edin.")
        issued = sum((from_db_text(x["quantity"]) for x in self.conn.execute(
            "SELECT quantity FROM stk_issues WHERE order_line_id = ?", (line_id,))), ZERO)
        if self._move_totals(line_id)[0] > issued:
            raise ValidationError(msg or "Depoya iade edilen miktar çıkış toplamından fazla olamaz.")

    # --- hareketler ----------------------------------------------------------------------
    def _move(self, line_id: int, kind: str, move_date: str, quantity, frm, to, doc_no: str, reason: str) -> int:
        row = self.row(line_id)
        for loc in (frm, to):
            if loc is not None:
                self._check_location(loc, row)
        d = validate_iso_date(move_date, "Hareket tarihi", required=True)
        first = self.conn.execute("SELECT MIN(d.delivery_date) FROM po_delivery_lines dl JOIN po_deliveries d ON d.id = "
                                  "dl.delivery_id WHERE dl.order_line_id = ?", (line_id,)).fetchone()[0]
        if first and d < first:
            raise ValidationError("Hareket tarihi ilk teslim tarihinden önce olamaz.")
        try:
            q = to_decimal(quantity)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Miktar geçerli bir sayı değil.") from exc
        with transaction(self.conn):
            mid = self.conn.execute(
                "INSERT INTO stk_moves(order_line_id, kind, move_date, quantity, from_location_id, to_location_id, doc_no, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (line_id, kind, d, to_db_text(q, 3), frm, to, clean(doc_no) or "",
                                                     clean(reason) or "")).lastrowid
            self._validate(line_id)
            audit.log(self.conn, "stk_move", mid, "EKLE", new={"line": line_id, "kind": kind, "qty": str(q), "item": row.description})
        return mid

    def return_to_stock(self, line_id: int, move_date: str, quantity, location_id: int | None = None, reason: str = "") -> int:
        """Çıkışın depoya iadesi (miktar). Maliyet değişmez; iade edilen miktar çıkış toplamını aşamaz."""
        if to_decimal(quantity) <= 0:
            raise ValidationError("İade miktarı sıfırdan büyük olmalıdır.")
        return self._move(line_id, "IADE_DEPO", move_date, quantity, None, location_id, "", reason)

    def transfer(self, line_id: int, move_date: str, quantity, from_location_id: int | None, to_location_id: int | None,
                 doc_no: str = "", reason: str = "") -> int:
        """Depolar arası transfer (boş uç = varsayılan depo). Toplam stok ve maliyet değişmez; aynı proje sınırı içinde kalır."""
        if to_decimal(quantity) <= 0:
            raise ValidationError("Transfer miktarı sıfırdan büyük olmalıdır.")
        if from_location_id == to_location_id:
            raise ValidationError("Kaynak ve hedef depo aynı olamaz.")
        return self._move(line_id, "TRANSFER", move_date, quantity, from_location_id, to_location_id, doc_no, reason)

    def adjust(self, line_id: int, move_date: str, delta, reason: str, location_id: int | None = None) -> int:
        """Sayım düzeltmesi (işaretli miktar, gerekçe zorunlu). Yalnız miktarı düzeltir; maliyet kaydı oluşturmaz."""
        if not clean(reason):
            raise ValidationError("Sayım düzeltmesi için gerekçe zorunludur.")
        if to_decimal(delta) == 0:
            raise ValidationError("Düzeltme miktarı sıfır olamaz.")
        return self._move(line_id, "SAYIM", move_date, delta, None, location_id, "", reason)

    def ledger(self, project_id: int | None = None, line_id: int | None = None) -> list[Movement]:
        """Hareket defteri: teslim girişleri, çıkışlar, iadeler, transferler, sayımlar (tarih sırasıyla)."""
        out: list[Movement] = []
        lines = {r["id"]: r for r in self.conn.execute(
            "SELECT l.id, o.order_no, o.project_id, l.description, l.unit FROM po_order_lines l JOIN po_orders o ON o.id = l.order_id"
            " WHERE o.status <> 'IPTAL' AND (? IS NULL OR o.project_id = ?)", (project_id, project_id))}
        names = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM stk_locations")}

        def nm(i):
            return DEFAULT_DEPOT if i is None else names.get(i, "?")

        def add(lid, d, kind, q, eff, frm, to, doc, note, delivery_id=None):
            if lid in lines and (line_id is None or lid == line_id):
                ln = lines[lid]
                out.append(Movement(d, kind, lid, ln["order_no"], ln["description"], ln["unit"], q, eff, frm, to, doc, note,
                                    delivery_id))

        for r in self.conn.execute("SELECT dl.order_line_id, dl.quantity, d.delivery_date, d.waybill_no, d.id AS delivery_id FROM po_delivery_lines dl"
                                   " JOIN po_deliveries d ON d.id = dl.delivery_id"):
            q = from_db_text(r["quantity"])
            add(r["order_line_id"], r["delivery_date"], "GIRIS", q, q, "Tedarikçi", DEFAULT_DEPOT, r["waybill_no"] or "", "",
                r["delivery_id"])
        for r in self.conn.execute("SELECT * FROM stk_issues"):
            q = from_db_text(r["quantity"])
            add(r["order_line_id"], r["issue_date"], "CIKIS", q, -q, nm(r["location_id"]) if r["location_id"] else (r["location"] or DEFAULT_DEPOT),
                "Kullanım", "", r["note"])
        for r in self.conn.execute("SELECT * FROM stk_moves"):
            q = from_db_text(r["quantity"])
            if r["kind"] == "IADE_DEPO":
                add(r["order_line_id"], r["move_date"], "IADE_DEPO", q, q, "Kullanım", nm(r["to_location_id"]), r["doc_no"], r["reason"])
            elif r["kind"] == "TRANSFER":
                add(r["order_line_id"], r["move_date"], "TRANSFER", q, ZERO, nm(r["from_location_id"]), nm(r["to_location_id"]),
                    r["doc_no"], r["reason"])
            else:
                add(r["order_line_id"], r["move_date"], "SAYIM", q, q, nm(r["to_location_id"]), nm(r["to_location_id"]), r["doc_no"], r["reason"])
        order = {"GIRIS": 0, "CIKIS": 1, "IADE_DEPO": 2, "TRANSFER": 3, "SAYIM": 4}
        return sorted(out, key=lambda m: (m.date, order[m.kind], m.line_id))
