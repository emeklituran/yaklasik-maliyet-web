"""Hakedişler: dönem miktarları, metraj, kesintiler, durum akışı (taslak → kontrol → onay → ödendi), düzeltme
revizyonu, kesinleşmiş kayıt (JSON + SHA-256) ve dönemler arası karşılaştırma.

Kurallar:
- Bir sözleşmede aynı anda tek açık (taslak/kontrol) hakediş olur; yeni hakediş için öncekinin onaylı olması gerekir.
- Önceki değerler bir önceki geçerli (onaylı/ödendi) hakedişin kesinleşmiş kaydından alınır.
- Onaylanan hakedişin içeriği değişmez (veritabanı tetikleyicileri de engeller). Düzeltme yalnızca son hakediş için,
  aynı numarada yeni revizyon olarak yapılır; yeni revizyon onaylanınca eskisi "Düzeltildi" durumuna geçer.
- Her durum değişikliği kullanıcı adı, zaman ve notla kaydedilir.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet import __version__
from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.migrator import current_version
from yapimmaliyet.db.repositories import audit, contract_changes
from yapimmaliyet.db.repositories import progress as repo
from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.domain.progress import (
    STATUSES,
    DeductionEntry,
    DeductionType,
    Item,
    LineInput,
    PaymentResult,
    Previous,
    PriceDiff,
    Terms,
    Warning,
    compute,
)
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.contract_service import ensure_not_final, summarize
from yapimmaliyet.services.progress_service import _dec
from yapimmaliyet.services.validators import clean, validate_iso_date

SNAPSHOT_SCHEMA = "yapimmaliyet.hakedis/1"
EFFECTIVE = ("ONAY", "ODENDI")
TRANSITIONS = {("TASLAK", "KONTROL"), ("KONTROL", "TASLAK"), ("KONTROL", "ONAY"), ("ONAY", "ODENDI")}


def default_user() -> str:
    try:
        return getpass.getuser() or "kullanıcı"
    except Exception:   # ortam değişkeni yoksa
        return "kullanıcı"


def _s(v) -> str | None:
    return None if v is None else str(v)


def _d(v) -> Decimal | None:
    return None if v is None else to_decimal(v)


@dataclass
class PaymentView:
    contract: repo.Contract
    payment: repo.Payment
    items: list[Item]
    deduction_types: list[DeductionType]
    entries: list[DeductionEntry]
    result: PaymentResult
    measurements: dict[int, list[dict]]
    history: list[dict]          # önceki geçerli hakedişlerin özet satırları (takip tablosu / hakediş özeti)
    status_log: list[dict]
    from_snapshot: bool
    contract_amount: Decimal | None = None   # hesapta kullanılan bedel (değişikliklerle güncel ya da kesinleşmiş)

    @property
    def draft(self) -> bool:
        return self.payment.status not in EFFECTIVE + ("DUZELTILDI",)

    @property
    def timeline(self) -> list[dict]:
        """Önceki geçerli hakedişlerin özetleri + bu hakediş (takip tablosu / hakediş özeti)."""
        return list(self.history) + [payment_summary(self)]


class ProgressPaymentService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- okuma -------------------------------------------------------------------
    def payments(self, contract_id: int) -> list[repo.Payment]:
        return repo.list_payments(self.conn, contract_id)

    def payment(self, payment_id: int) -> repo.Payment:
        p = repo.get_payment(self.conn, payment_id)
        if p is None:
            raise NotFoundError("Hakediş bulunamadı.")
        return p

    def _effective(self, contract_id: int, before_no: int | None = None) -> list[repo.Payment]:
        """Her numaranın geçerli (onaylı/ödendi) revizyonu, numara sırasıyla."""
        out = [p for p in repo.list_payments(self.conn, contract_id) if p.status in EFFECTIVE]
        return [p for p in out if before_no is None or p.no < before_no]

    def contract_totals(self, contract_id: int) -> dict:
        """Geçerli (onaylı/ödendi) hakedişlerin kesinleşmiş kayıtlarından sözleşme toplamları: son kümülatif iş ve
        fiyat farkı, onaylı ödenecek tutarlar toplamı, ödendi işaretli olanların toplamı. Kayıt bozuksa hata verir."""
        snaps = [(p, self._snapshot(p)) for p in self._effective(contract_id)]
        zero = Decimal(0)
        last = snaps[-1][1]["totals"] if snaps else {}
        return {"approved": len(snaps), "paid_count": sum(1 for p, _ in snaps if p.status == "ODENDI"),
                "work_cum": to_decimal(last.get("work_cum", "0")),
                "price_diff_cum": to_decimal(last.get("price_diff_cum", "0")),
                "payable": sum((to_decimal(s["totals"]["payable"]) for _, s in snaps), zero),
                "paid": sum((to_decimal(s["totals"]["payable"]) for p, s in snaps if p.status == "ODENDI"), zero),
                "open": any(p.status in ("TASLAK", "KONTROL") for p in repo.list_payments(self.conn, contract_id))}

    def effective_snapshots(self, contract_id: int) -> list[tuple[repo.Payment, dict]]:
        """Geçerli (onaylı/ödendi) hakedişler ve bütünlüğü denetlenmiş kesinleşmiş kayıtları, numara sırasıyla."""
        return [(p, self._snapshot(p)) for p in self._effective(contract_id)]

    def _snapshot(self, p: repo.Payment) -> dict:
        if hashlib.sha256(p.snapshot_json.encode("utf-8")).hexdigest() != p.snapshot_sha:
            raise ConflictError(f"{p.label} kesinleşmiş kaydının bütünlük özeti uyuşmuyor; kayıt bozulmuş olabilir.")
        return json.loads(p.snapshot_json)

    def _previous(self, contract_id: int, no: int) -> tuple[Previous, dict[int, Decimal], list[dict]]:
        eff = self._effective(contract_id, before_no=no)
        if not eff:
            return Previous(), {}, []
        snaps = [self._snapshot(p) for p in eff]
        last = snaps[-1]["totals"]
        prev = Previous(to_decimal(last["work_cum"]), to_decimal(last["price_diff_cum"]),
                        to_decimal(last["advance_cum"]))
        qtys = {ln["item_id"]: to_decimal(ln["cum_qty"]) for ln in snaps[-1]["lines"]}
        return prev, qtys, [s["summary"] for s in snaps]

    def view(self, payment_id: int) -> PaymentView:
        p = self.payment(payment_id)
        contract = repo.get_contract(self.conn, p.contract_id)
        if p.snapshot_json:
            return self._view_from_snapshot(contract, p)
        items = repo.list_items(self.conn, p.contract_id)
        types = repo.list_deduction_types(self.conn, p.contract_id)
        prev, prev_qty, history = self._previous(p.contract_id, p.no)
        qtys = repo.line_qtys(self.conn, payment_id)
        lines = [LineInput(it, prev_qty.get(it.id, Decimal(0)), qtys.get(it.id, (Decimal(0), None))[0])
                 for it in items]
        entries = [DeductionEntry(r["type_id"], to_decimal(r["amount"]), r["description"])
                   for r in repo.deduction_rows(self.conn, payment_id)]
        current = summarize(contract, contract_changes.list_changes(self.conn, contract.id))
        terms = Terms(contract.model, current.current_amount, contract.vat_rate, contract.vat_base,
                      contract.advance_total)
        result = compute(terms, lines, types, entries, self._price_diff(p), prev)
        meas = self._measurements(payment_id)
        result.warnings += self._extra_checks(contract, p, items, qtys, meas, history, current.current_end)
        return PaymentView(contract, p, items, types, entries, result, meas, history, self._log(payment_id), False,
                           current.current_amount)

    def line_notes(self, payment_id: int) -> dict[int, str | None]:
        """Satır açıklamaları (taslakta veritabanından, onaylıda kesinleşmiş kayıttan bağımsız olarak saklanan not)."""
        return {item_id: note for item_id, (_, note) in repo.line_qtys(self.conn, payment_id).items()}

    @staticmethod
    def _price_diff(p: repo.Payment) -> PriceDiff:
        prm = p.price_diff_params or {}
        return PriceDiff(p.price_diff_mode, p.price_diff_amount, _d(prm.get("pn")), _d(prm.get("b")))

    def _measurements(self, payment_id: int) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for r in repo.measurements(self.conn, payment_id):
            out.setdefault(r["item_id"], []).append({"description": r["description"], "quantity": r["quantity"],
                                                     "ref": r["ref"]})
        return out

    def _log(self, payment_id: int) -> list[dict]:
        return [dict(r) for r in repo.status_log(self.conn, payment_id)]

    def _extra_checks(self, c: repo.Contract, p: repo.Payment, items: list[Item], qtys: dict, meas: dict,
                      history: list[dict], end_date: str | None) -> list[Warning]:
        w: list[Warning] = []
        known = {it.id for it in items}
        for item_id, (qty, note) in qtys.items():
            if item_id in known and qty < 0 and not clean(note):
                w.append(Warning("UYARI", "NEGATIF_ACIKLAMA", "Negatif miktarlı satıra açıklama girilmemiş.", item_id))
        for item_id, rows in meas.items():
            total = sum((to_decimal(r["quantity"]) for r in rows), Decimal(0))
            if qtys.get(item_id, (Decimal(0),))[0] != total:
                w.append(Warning("HATA", "METRAJ_TOPLAM", "Metraj satırları toplamı bu dönem miktarına eşit değil.",
                                 item_id))
            keys = [(r["description"].strip().casefold(), str(to_decimal(r["quantity"]))) for r in rows]
            if len(keys) != len(set(keys)):
                w.append(Warning("UYARI", "MUKERRER_METRAJ", "Aynı metraj satırı bu hakedişte iki kez girilmiş.",
                                 item_id))
        earlier = {(r["item_id"], r["description"].strip().casefold(), str(to_decimal(r["quantity"]))): r["no"]
                   for r in repo.earlier_measurements(self.conn, c.id, p.id)}
        for item_id, rows in meas.items():
            for r in rows:
                no = earlier.get((item_id, r["description"].strip().casefold(), str(to_decimal(r["quantity"]))))
                if no is not None:
                    w.append(Warning("UYARI", "MUKERRER_IMALAT", f"'{r['description']}' metrajı {no} nolu hakedişte "
                                     "aynı miktarla zaten ödenmiş görünüyor (mükerrer imalat olabilir).", item_id))
        if p.period_start and p.period_start > p.period_end:
            w.append(Warning("HATA", "DONEM", "Dönem başlangıcı dönem sonundan sonra olamaz."))
        if history and history[-1].get("period_end") and p.period_end <= history[-1]["period_end"]:
            w.append(Warning("HATA", "DONEM", "Dönem sonu bir önceki hakedişin dönem sonundan sonra olmalıdır."))
        if end_date and p.period_end > end_date:
            w.append(Warning("UYARI", "SURE", "Dönem sonu sözleşmenin (süre uzatımları dahil) bitiş tarihinden sonra "
                             "(süre uzatımı kaydı?)."))
        if not items:
            w.append(Warning("HATA", "KALEM_YOK", "Sözleşmede kalem tanımlı değil."))
        return w

    def _view_from_snapshot(self, contract: repo.Contract, p: repo.Payment) -> PaymentView:
        s = self._snapshot(p)
        items = [Item(i["id"], i["line_no"], i["code"], i["description"], i["unit"], i["discipline"], i["group_name"],
                      i["method"], _d(i["contract_qty"]), _d(i["unit_price"]), _d(i["lump_amount"]), i["source"])
                 for i in s["items"]]
        by_id = {it.id: it for it in items}
        lines = [LineInput(by_id[ln["item_id"]], to_decimal(ln["prev_qty"]), to_decimal(ln["period_qty"]))
                 for ln in s["lines"]]
        types = [DeductionType(t["id"], t["name"], t["base"], _d(t["rate"]), t["is_advance"], t["is_guarantee"])
                 for t in s["deduction_types"]]
        entries = [DeductionEntry(e["type_id"], to_decimal(e["amount"]), e["description"]) for e in s["entries"]]
        t, pv, pd = s["terms"], s["previous"], s["price_diff"]
        terms = Terms(t["model"], to_decimal(t["contract_amount"]), to_decimal(t["vat_rate"]), t["vat_base"],
                      to_decimal(t["advance_total"]))
        result = compute(terms, lines, types, entries, PriceDiff(pd["mode"], _d(pd["amount"]), _d(pd["pn"]),
                                                                 _d(pd["b"])),
                         Previous(to_decimal(pv["work_cum"]), to_decimal(pv["price_diff_cum"]),
                                  to_decimal(pv["advance_cum"])))
        if str(result.payable) != s["totals"]["payable"]:
            raise ConflictError(f"{p.label}: kesinleşmiş kayıttan yeniden hesaplanan tutar kayıtlı tutarla uyuşmuyor.")
        result.warnings = [Warning(**w) for w in s["warnings"]]
        return PaymentView(contract, p, items, types, entries, result, {int(k): v for k, v in s["measurements"].items()},
                           s["history"], self._log(p.id), True, terms.contract_amount)

    # --- oluşturma ve düzenleme --------------------------------------------------
    def create_payment(self, contract_id: int, period_end: str, period_start: str | None = None,
                       payment_date: str | None = None) -> int:
        if repo.get_contract(self.conn, contract_id) is None:
            raise NotFoundError("Hakediş sözleşmesi bulunamadı.")
        ensure_not_final(self.conn, contract_id)
        pays = repo.list_payments(self.conn, contract_id)
        if any(p.status in ("TASLAK", "KONTROL") for p in pays):
            raise ConflictError("Açık (taslak/kontrolde) bir hakediş var; önce onu onaylayın ya da silin.")
        eff = self._effective(contract_id)
        no = (eff[-1].no + 1) if eff else 1
        pe = validate_iso_date(period_end, "Dönem sonu", required=True)
        ps = validate_iso_date(period_start, "Dönem başlangıcı") or (eff[-1].period_end if eff else None)
        with transaction(self.conn):
            pid = repo.insert_payment(self.conn, contract_id, no, 0, None, ps, pe,
                                      validate_iso_date(payment_date, "Hakediş tarihi") or pe)
            repo.log_status(self.conn, pid, None, "TASLAK", default_user(), "Hakediş oluşturuldu")
            audit.log(self.conn, "pp_payment", pid, "EKLE", new={"contract": contract_id, "no": no})
        return pid

    def _draft(self, payment_id: int) -> repo.Payment:
        p = self.payment(payment_id)
        if p.status != "TASLAK":
            raise ConflictError(f"{p.label} {STATUSES[p.status].lower()}; yalnızca taslak hakediş düzenlenebilir.")
        return p

    def update_header(self, payment_id: int, period_end: str, period_start: str | None, payment_date: str | None,
                      price_diff_mode: str = "YOK", price_diff_amount=None, pn=None, b=None,
                      price_diff_note: str | None = None, notes: str | None = None) -> None:
        self._draft(payment_id)
        if price_diff_mode not in ("YOK", "TUTAR", "KATSAYI"):
            raise ValidationError("Geçersiz fiyat farkı yöntemi.")
        amount = _dec(price_diff_amount, "Fiyat farkı tutarı", allow_none=True, min_zero=False) \
            if price_diff_mode == "TUTAR" else None
        params = {}
        if price_diff_mode == "KATSAYI":
            params = {"pn": str(_dec(pn, "Pn")), "b": str(_dec(b, "B katsayısı"))}
        with transaction(self.conn):
            repo.update_payment_header(self.conn, payment_id, validate_iso_date(period_start, "Dönem başlangıcı"),
                                       validate_iso_date(period_end, "Dönem sonu", required=True),
                                       validate_iso_date(payment_date, "Hakediş tarihi"), price_diff_mode, amount,
                                       params, clean(price_diff_note), clean(notes))

    def _check_item(self, p: repo.Payment, item_id: int) -> Item:
        found = repo.get_item(self.conn, item_id)
        if found is None or found[0] != p.contract_id:
            raise ValidationError("Kalem bu sözleşmeye ait değil.")
        return found[1]

    def set_quantity(self, payment_id: int, item_id: int, qty, note: str | None = None) -> None:
        """Bu dönem miktarı (götürü kalemde oran artışı, ör. 0,15 = %15). Boş değer satırı kaldırır."""
        p = self._draft(payment_id)
        it = self._check_item(p, item_id)
        q = _dec(qty, "Bu dönem miktarı", allow_none=True, min_zero=False)
        if q is not None and it.method == "GOTURU" and abs(q) > 1:
            raise ValidationError("Götürü kalemde oran kesir olarak girilir (ör. %15 için 0,15).")
        if repo.measurements(self.conn, payment_id, item_id):
            raise ConflictError("Bu kalemin metraj satırları var; miktar metraj cetvelinden hesaplanır.")
        with transaction(self.conn):
            repo.set_line(self.conn, payment_id, item_id, q, clean(note))

    def set_measurements(self, payment_id: int, item_id: int, rows: list[tuple[str, object, str | None]]) -> Decimal:
        """Metraj / ataşman satırları; satırın bu dönem miktarı bunların toplamı olur."""
        p = self._draft(payment_id)
        self._check_item(p, item_id)
        clean_rows = []
        for desc, qty, ref in rows:
            d = clean(desc)
            if not d:
                raise ValidationError("Metraj satırının açıklaması boş bırakılamaz.")
            clean_rows.append((d, _dec(qty, f"'{d}' miktarı", min_zero=False), clean(ref)))
        total = sum((q for _, q, _ in clean_rows), Decimal(0))
        old_note = repo.line_qtys(self.conn, payment_id).get(item_id, (None, None))[1]
        with transaction(self.conn):
            repo.replace_measurements(self.conn, payment_id, item_id, clean_rows)
            repo.set_line(self.conn, payment_id, item_id, total if clean_rows else None, old_note)
        return total

    def set_deductions(self, payment_id: int, rows: list[tuple[int, str | None, object]]) -> None:
        """Tutar girilen (MANUEL) kesinti satırları; oranlı kesintiler hesaplanır, buraya girilmez."""
        p = self._draft(payment_id)
        types = {t.id: t for t in repo.list_deduction_types(self.conn, p.contract_id)}
        out = []
        for type_id, desc, amount in rows:
            t = types.get(type_id)
            if t is None:
                raise ValidationError("Kesinti türü bu sözleşmede tanımlı ya da etkin değil.")
            if t.base != "MANUEL":
                raise ValidationError(f"'{t.name}' oranla hesaplanır; tutar girilemez.")
            out.append((type_id, clean(desc), _dec(amount, f"'{t.name}' tutarı", min_zero=False)))
        with transaction(self.conn):
            repo.replace_deductions(self.conn, payment_id, out)

    def delete_payment(self, payment_id: int) -> None:
        p = self.payment(payment_id)
        if p.status not in ("TASLAK", "KONTROL"):
            raise ConflictError("Onaylanmış hakediş silinemez.")
        with transaction(self.conn):
            if p.status == "KONTROL":
                repo.set_status(self.conn, payment_id, "TASLAK")
            repo.delete_payment(self.conn, payment_id)
            audit.log(self.conn, "pp_payment", payment_id, "SIL", old={"no": p.no, "revision": p.revision})

    # --- durum akışı -------------------------------------------------------------
    def transition(self, payment_id: int, to_status: str, user: str | None = None, note: str | None = None) -> None:
        p = self.payment(payment_id)
        if to_status in ("ONAY", "ODENDI"):
            authz.require("HAKEDIS", authz.APPROVE, "hakediş onayı / ödeme")
        if (p.status, to_status) not in TRANSITIONS:
            raise ConflictError(f"{STATUSES[p.status]} durumundan {STATUSES.get(to_status, to_status)} durumuna "
                                "geçilemez.")
        user = clean(user) or default_user()
        if to_status in ("KONTROL", "ONAY"):
            v = self.view(payment_id)
            if v.result.errors:
                raise ConflictError("Hakedişte giderilmesi gereken hatalar var:\n" +
                                    "\n".join(w.message for w in v.result.errors[:8]))
        with transaction(self.conn):
            if to_status == "ONAY":
                text = json.dumps(self._payload(v, user, note), ensure_ascii=False, sort_keys=True)
                repo.set_status(self.conn, payment_id, "ONAY", text, hashlib.sha256(text.encode("utf-8")).hexdigest())
                if p.supersedes_id:
                    repo.set_status(self.conn, p.supersedes_id, "DUZELTILDI", keep_snapshot=True)
                    repo.log_status(self.conn, p.supersedes_id, None, "DUZELTILDI", user,
                                    f"Yerine {p.label} onaylandı")
            else:
                repo.set_status(self.conn, payment_id, to_status, keep_snapshot=to_status == "ODENDI")
            repo.log_status(self.conn, payment_id, p.status, to_status, user, clean(note))
            audit.log(self.conn, "pp_payment", payment_id, "GUNCELLE", old={"status": p.status},
                      new={"status": to_status, "user": user}, note=clean(note))

    def create_correction(self, payment_id: int, reason: str, user: str | None = None) -> int:
        """Son geçerli hakediş için aynı numarada yeni revizyon (taslak, içerik kopyalanır)."""
        p = self.payment(payment_id)
        if p.status not in EFFECTIVE:
            raise ConflictError("Yalnızca onaylanmış/ödenmiş hakediş için düzeltme revizyonu oluşturulur.")
        ensure_not_final(self.conn, p.contract_id)
        reason = clean(reason)
        if not reason:
            raise ValidationError("Düzeltme gerekçesi girilmelidir.")
        pays = repo.list_payments(self.conn, p.contract_id)
        if any(x.no > p.no for x in pays) or any(x.status in ("TASLAK", "KONTROL") for x in pays):
            raise ConflictError("Yalnızca son hakediş düzeltilebilir ve açık hakediş olmamalıdır. Önceki dönem "
                                "hataları bir sonraki hakedişte düzeltme (negatif miktar) olarak işlenir.")
        rev = max(x.revision for x in pays if x.no == p.no) + 1
        with transaction(self.conn):
            nid = repo.insert_payment(self.conn, p.contract_id, p.no, rev, p.id, p.period_start, p.period_end,
                                      p.payment_date, p.price_diff_mode, p.price_diff_amount, p.price_diff_params,
                                      p.price_diff_note, p.notes)
            repo.copy_payment_content(self.conn, p.id, nid)
            repo.log_status(self.conn, nid, None, "TASLAK", clean(user) or default_user(), f"Düzeltme: {reason}")
            audit.log(self.conn, "pp_payment", nid, "EKLE", new={"supersedes": p.id, "revision": rev}, note=reason)
        return nid

    # --- kesinleşmiş kayıt ve karşılaştırma ---------------------------------------
    def _payload(self, v: PaymentView, user: str, note: str | None) -> dict:
        r, c, p = v.result, v.contract, v.payment
        return {
            "schema": SNAPSHOT_SCHEMA, "app_version": __version__, "db_schema": current_version(self.conn),
            "approved_by": user, "approval_note": clean(note),
            "contract": {"id": c.id, "kind": c.kind, "title": c.title, "employer": c.employer,
                         "contractor": c.contractor, "contract_no": c.contract_no, "contract_date": c.contract_date,
                         "ikn": c.ikn, "currency": c.currency},
            "payment": {"id": p.id, "no": p.no, "revision": p.revision, "period_start": p.period_start,
                        "period_end": p.period_end, "payment_date": p.payment_date, "notes": p.notes},
            "terms": {"model": c.model, "contract_amount": _s(v.contract_amount), "vat_rate": _s(c.vat_rate),
                      "vat_base": c.vat_base, "advance_total": _s(c.advance_total)},
            "items": [{"id": it.id, "line_no": it.line_no, "code": it.code, "description": it.description,
                       "unit": it.unit, "discipline": it.discipline, "group_name": it.group_name, "method": it.method,
                       "contract_qty": _s(it.contract_qty), "unit_price": _s(it.unit_price),
                       "lump_amount": _s(it.lump_amount), "source": it.source,
                       "source_ref": repo.item_source_ref(self.conn, it.id)} for it in v.items],
            "lines": [{"item_id": ln.item.id, "prev_qty": _s(ln.input.prev_qty), "period_qty": _s(ln.input.period_qty),
                       "cum_qty": _s(ln.cum_qty), "cum_amount": _s(ln.cum_amount), "prev_amount": _s(ln.prev_amount),
                       "period_amount": _s(ln.period_amount)} for ln in r.lines],
            "measurements": {str(k): rows for k, rows in v.measurements.items()},
            "deduction_types": [{"id": t.id, "name": t.name, "base": t.base, "rate": _s(t.rate),
                                 "is_advance": t.is_advance, "is_guarantee": t.is_guarantee} for t in v.deduction_types],
            "entries": [{"type_id": e.type_id, "amount": _s(e.amount), "description": e.description} for e in v.entries],
            "price_diff": {"mode": p.price_diff_mode, "amount": _s(p.price_diff_amount),
                           "pn": (p.price_diff_params or {}).get("pn"), "b": (p.price_diff_params or {}).get("b"),
                           "note": p.price_diff_note},
            "previous": {"work_cum": _s(r.work_prev), "price_diff_cum": _s(r.price_diff_prev),
                         "advance_cum": _s(r.advance_cum - r.advance_period)},
            "totals": {"work_cum": _s(r.work_cum), "work_period": _s(r.work_period),
                       "price_diff_period": _s(r.price_diff_period), "price_diff_cum": _s(r.price_diff_cum),
                       "this_total": _s(r.this_total), "vat": _s(r.vat), "deductions_total": _s(r.deductions_total),
                       "payable": _s(r.payable), "advance_cum": _s(r.advance_cum),
                       "remaining_contract": _s(r.remaining_contract)},
            "warnings": [{"level": w.level, "code": w.code, "message": w.message, "item_id": w.item_id}
                         for w in r.warnings],
            "history": v.history, "summary": payment_summary(v),
        }

    def compare(self, payment_a: int, payment_b: int) -> list[dict]:
        """İki hakedişin kalem bazında toplam miktar ve tutar farkı (aynı sözleşme)."""
        va, vb = self.view(payment_a), self.view(payment_b)
        if va.contract.id != vb.contract.id:
            raise ValidationError("Karşılaştırılan hakedişler aynı sözleşmeye ait olmalıdır.")
        a = {ln.item.id: ln for ln in va.result.lines}
        out = []
        for ln in vb.result.lines:
            x = a.get(ln.item.id)
            qa, ma = (x.cum_qty, x.cum_amount or Decimal(0)) if x else (Decimal(0), Decimal(0))
            out.append({"item": ln.item, "qty_a": qa, "qty_b": ln.cum_qty, "amount_a": ma,
                        "amount_b": ln.cum_amount or Decimal(0), "qty_diff": ln.cum_qty - qa,
                        "amount_diff": (ln.cum_amount or Decimal(0)) - ma})
        return out


def payment_summary(v: PaymentView) -> dict:
    """Takip tablosu / hakediş özeti satırı (kesinleşmiş kayda da yazılır)."""
    r, p = v.result, v.payment
    return {"no": p.no, "revision": p.revision, "period_start": p.period_start, "period_end": p.period_end,
            "payment_date": p.payment_date, "work_period": _s(r.work_period), "price_diff": _s(r.price_diff_period),
            "this_total": _s(r.this_total), "vat": _s(r.vat),
            "deductions": {d.type.name: _s(d.amount) for d in r.deductions},
            "deductions_total": _s(r.deductions_total), "payable": _s(r.payable), "work_cum": _s(r.work_cum)}
