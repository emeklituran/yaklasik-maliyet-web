"""Kesin hesap: sözleşmenin son geçerli hakedişinden kalem bazında kesin miktar ve tutarlar, değişikliklerle güncel
bedel, ödenenler, kesintiler ve iade edilecek teminat.

Kurallar:
- Veri yeniden girilmez: kalemler ve kümülatif miktarlar son geçerli (onaylı/ödendi) hakedişin kesinleşmiş
  kaydından, ödenen ve kesinti toplamları bütün geçerli hakedişlerin kayıtlarından, güncel bedel onaylı sözleşme
  değişikliklerinden okunur.
- Açık (taslak/kontrol) hakediş varken ya da hiç onaylı hakediş yokken kesinleştirilemez; işin bitim (geçici kabul)
  tarihi girilmelidir.
- Kesinleşen kesin hesap JSON + SHA-256 ile sabitlenir; sonra sözleşmede hakediş, düzeltme ve değişiklik kaydı
  açılamaz (`ensure_not_final`).
- Uyarılar (karar kullanıcıda): avans tam mahsup edilmemiş, iş tutarı güncel bedeli aşıyor, onay bekleyen değişiklik.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.migrator import current_version
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.infra.errors import ConflictError, ValidationError
from yapimmaliyet.services.contract_service import ContractService, ContractSummary
from yapimmaliyet.services.progress_payment_service import ProgressPaymentService, default_user
from yapimmaliyet.services.validators import clean, validate_iso_date

SCHEMA = "yapimmaliyet.kesin_hesap/1"
ZERO = Decimal(0)


@dataclass(frozen=True)
class FinalLine:
    line_no: int
    code: str | None
    description: str
    unit: str | None
    method: str
    contract_qty: Decimal | None
    unit_price: Decimal | None
    contract_amount: Decimal       # birim fiyatlıda round(miktar × fiyat, 2); götürüde kalem bedeli
    final_qty: Decimal             # götürüde gerçekleşme oranı
    final_amount: Decimal
    source: str

    @property
    def difference(self) -> Decimal:
        return self.final_amount - self.contract_amount


@dataclass
class FinalAccount:
    summary: ContractSummary
    record: dict | None             # fa_final_accounts satırı (yoksa None)
    lines: list[FinalLine]
    payments: list[dict]            # geçerli hakediş özetleri
    work_total: Decimal
    price_diff_total: Decimal
    vat_total: Decimal
    payable_total: Decimal
    paid_total: Decimal
    deductions: dict[str, Decimal]  # kesinti adı → toplam
    guarantee_total: Decimal        # teminat niteliğindeki kesintiler (kesin kabulde iade)
    advance_total: Decimal
    advance_recovered: Decimal
    open_payment: bool
    warnings: list[str] = field(default_factory=list)
    from_snapshot: bool = False
    snapshot_amounts: dict | None = None   # kesinleşmiş kayıttaki sözleşme ve güncel bedel

    @property
    def current_amount(self) -> Decimal:
        return self.snapshot_amounts["current_amount"] if self.snapshot_amounts else self.summary.current_amount

    @property
    def final(self) -> bool:
        return bool(self.record) and self.record["status"] == "KESIN"

    @property
    def difference(self) -> Decimal:
        """Kesin iş tutarı − güncel sözleşme bedeli (negatif: iş eksilişi)."""
        return self.work_total - self.current_amount

    @property
    def unpaid(self) -> Decimal:
        """Onaylı ama ödendi işaretlenmemiş hakediş tutarları."""
        return self.payable_total - self.paid_total

    @property
    def blockers(self) -> list[str]:
        out = []
        if self.open_payment:
            out.append("Açık (taslak/kontrol) hakediş var; önce onaylayın ya da silin.")
        if not self.payments:
            out.append("Onaylı hakediş yok; kesin hesap son geçerli hakedişe dayanır.")
        return out


def _d(v) -> Decimal:
    return ZERO if v in (None, "") else to_decimal(v)


class FinalAccountService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.contracts = ContractService(conn)
        self.pay = ProgressPaymentService(conn)

    def record(self, contract_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM fa_final_accounts WHERE contract_id = ?", (contract_id,)).fetchone()
        return dict(r) if r else None

    def build(self, contract_id: int) -> FinalAccount:
        s = self.contracts.summary(contract_id)
        rec = self.record(contract_id)
        snaps = self.pay.effective_snapshots(contract_id)
        open_payment = any(p.status in ("TASLAK", "KONTROL") for p in self.pay.payments(contract_id))
        lines: list[FinalLine] = []
        if snaps:
            last = snaps[-1][1]
            cum = {ln["item_id"]: ln for ln in last["lines"]}
            for it in last["items"]:
                ln = cum.get(it["id"], {})
                qty, price, lump = _d(it["contract_qty"]), _d(it["unit_price"]), _d(it["lump_amount"])
                amount = lump if it["method"] == "GOTURU" else (qty * price).quantize(Decimal("0.01"))
                lines.append(FinalLine(it["line_no"], it["code"], it["description"], it["unit"], it["method"],
                                       None if it["contract_qty"] is None else qty,
                                       None if it["unit_price"] is None else price, amount,
                                       _d(ln.get("cum_qty")), _d(ln.get("cum_amount")), it["source"]))
        deductions: dict[str, Decimal] = {}
        guarantee = advance = ZERO
        for _, snap in snaps:
            kinds = {t["name"]: t for t in snap.get("deduction_types", [])}
            for name, amount in snap["summary"].get("deductions", {}).items():
                a = _d(amount)
                deductions[name] = deductions.get(name, ZERO) + a
                t = kinds.get(name, {})
                if t.get("is_guarantee"):
                    guarantee += a
                if t.get("is_advance"):
                    advance += a
        tot = (lambda key: sum((_d(sn["totals"][key]) for _, sn in snaps), ZERO))
        last_tot = snaps[-1][1]["totals"] if snaps else {}
        fa = FinalAccount(
            s, rec, lines, [sn["summary"] for _, sn in snaps], _d(last_tot.get("work_cum")),
            _d(last_tot.get("price_diff_cum")), tot("vat"), tot("payable"),
            sum((_d(sn["totals"]["payable"]) for p, sn in snaps if p.status == "ODENDI"), ZERO), deductions, guarantee,
            s.contract.advance_total, advance, open_payment)
        if fa.advance_total and advance < fa.advance_total:
            fa.warnings.append(f"Avansın {fa.advance_total - advance} TL'si mahsup edilmemiş.")
        if fa.payments and fa.work_total > s.current_amount:
            fa.warnings.append(f"Kesin iş tutarı güncel sözleşme bedelini {fa.work_total - s.current_amount} TL "
                               "aşıyor; iş artışı onayını denetleyin.")
        if s.pending:
            fa.warnings.append(f"{len(s.pending)} sözleşme değişikliği onay bekliyor; kesin hesaba dahil değil.")
        if fa.unpaid:
            fa.warnings.append(f"Onaylı hakedişlerden {fa.unpaid} TL henüz ödendi işaretlenmemiş.")
        over = [ln for ln in lines if ln.method == "BIRIM_FIYAT" and ln.contract_qty is not None
                and ln.final_qty > ln.contract_qty]
        if over:
            fa.warnings.append(f"{len(over)} kalemde kesin miktar sözleşme miktarını aşıyor.")
        if fa.final:
            return self._from_snapshot(fa)
        return fa

    def _from_snapshot(self, live: FinalAccount) -> FinalAccount:
        """Kesinleşmiş kesin hesap kayıttan okunur (bütünlük özeti denetlenir)."""
        rec = live.record
        if hashlib.sha256(rec["snapshot_json"].encode("utf-8")).hexdigest() != rec["snapshot_sha"]:
            raise ConflictError("Kesin hesap kaydının bütünlük özeti uyuşmuyor; kayıt bozulmuş olabilir.")
        data = json.loads(rec["snapshot_json"])
        t = data["totals"]
        lines = [FinalLine(x["line_no"], x["code"], x["description"], x["unit"], x["method"],
                           None if x["contract_qty"] is None else _d(x["contract_qty"]),
                           None if x["unit_price"] is None else _d(x["unit_price"]), _d(x["contract_amount"]),
                           _d(x["final_qty"]), _d(x["final_amount"]), x["source"]) for x in data["lines"]]
        fa = FinalAccount(live.summary, rec, lines, data["payments"], _d(t["work_total"]), _d(t["price_diff_total"]),
                          _d(t["vat_total"]), _d(t["payable_total"]), _d(t["paid_total"]),
                          {k: _d(v) for k, v in data["deductions"].items()}, _d(t["guarantee_total"]),
                          _d(t["advance_total"]), _d(t["advance_recovered"]), False, data["warnings"], True)
        fa.snapshot_amounts = {"contract_amount": _d(t["contract_amount"]), "current_amount": _d(t["current_amount"])}
        return fa

    def save_info(self, contract_id: int, completion_date: str | None, acceptance_ref: str | None,
                  notes: str | None) -> None:
        self.contracts.summary(contract_id)
        rec = self.record(contract_id)
        if rec and rec["status"] == "KESIN":
            raise ConflictError("Kesinleşmiş kesin hesap değiştirilemez.")
        vals = (validate_iso_date(completion_date, "İşin bitim (geçici kabul) tarihi"), clean(acceptance_ref),
                clean(notes))
        with transaction(self.conn):
            if rec:
                self.conn.execute("UPDATE fa_final_accounts SET completion_date = ?, acceptance_ref = ?, notes = ?"
                                  " WHERE id = ?", vals + (rec["id"],))
                fid = rec["id"]
            else:
                fid = self.conn.execute("INSERT INTO fa_final_accounts(contract_id, completion_date, acceptance_ref,"
                                        " notes) VALUES (?, ?, ?, ?)", (contract_id,) + vals).lastrowid
            audit.log(self.conn, "fa_final_account", fid, "GUNCELLE",
                      new={"contract": contract_id, "completion_date": vals[0], "acceptance_ref": vals[1]})

    def finalize(self, contract_id: int, user: str | None = None) -> str:
        """Kesin hesabı kesinleştirir; SHA-256 özetini döndürür."""
        fa = self.build(contract_id)
        if fa.final:
            raise ConflictError("Kesin hesap zaten kesinleşmiş.")
        if fa.blockers:
            raise ValidationError(" ".join(fa.blockers))
        if not fa.record or not fa.record["completion_date"]:
            raise ValidationError("Kesinleştirmeden önce işin bitim (geçici kabul) tarihini girin.")
        user = clean(user) or default_user()
        payload = self._payload(fa, user)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with transaction(self.conn):
            self.conn.execute("UPDATE fa_final_accounts SET status = 'KESIN', approved_by = ?, approved_at ="
                              " strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'), snapshot_json = ?, snapshot_sha = ?"
                              " WHERE id = ?", (user, text, sha, fa.record["id"]))
            audit.log(self.conn, "fa_final_account", fa.record["id"], "GUNCELLE",
                      new={"status": "KESIN", "sha": sha, "work_total": str(fa.work_total)}, note="Kesin hesap")
        return sha

    def _payload(self, fa: FinalAccount, user: str) -> dict:
        s, c = fa.summary, fa.summary.contract
        st = (lambda v: None if v is None else str(v))
        return {
            "schema": SCHEMA, "app_version": __version__, "db_schema": current_version(self.conn),
            "approved_by": user, "contract": {"id": c.id, "kind": c.kind, "title": c.title, "employer": c.employer,
                                              "contractor": c.contractor, "contract_no": c.contract_no,
                                              "currency": c.currency},
            "record": {k: fa.record[k] for k in ("completion_date", "acceptance_ref", "notes")},
            "lines": [{"line_no": ln.line_no, "code": ln.code, "description": ln.description, "unit": ln.unit,
                       "method": ln.method, "contract_qty": st(ln.contract_qty), "unit_price": st(ln.unit_price),
                       "contract_amount": st(ln.contract_amount), "final_qty": st(ln.final_qty),
                       "final_amount": st(ln.final_amount), "source": ln.source} for ln in fa.lines],
            "payments": fa.payments, "deductions": {k: str(v) for k, v in fa.deductions.items()},
            "totals": {"contract_amount": str(c.contract_amount), "current_amount": str(s.current_amount),
                       "work_total": str(fa.work_total), "price_diff_total": str(fa.price_diff_total),
                       "vat_total": str(fa.vat_total), "payable_total": str(fa.payable_total),
                       "paid_total": str(fa.paid_total), "guarantee_total": str(fa.guarantee_total),
                       "advance_total": str(fa.advance_total), "advance_recovered": str(fa.advance_recovered)},
            "warnings": fa.warnings,
        }
