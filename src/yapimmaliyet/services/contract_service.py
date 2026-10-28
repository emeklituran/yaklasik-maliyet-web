"""Proje düzeyinde sözleşmeler ve sözleşme değişiklikleri.

Ayrı bir sözleşme tablosu yoktur: hakediş sözleşmeleri (`pp_contracts`, taşeron ve idari) proje sözleşmesi olarak
listelenir. Değişiklik kayıtları (iş artışı / eksilişi, süre uzatımı, ek protokol) asıl sözleşme bedelini ve bitiş
tarihini değiştirmez; güncel bedel ve güncel bitiş tarihi yalnızca ONAYLI kayıtlardan hesaplanır. TASLAK (onay
bekleyen) kayıt izlenir ama hesaba girmez; onaylanınca bir kez ve yalnızca toplam içinde yansır. Taslak hakedişler güncel bedel ve
bitiş tarihini kullanır; onaylı hakedişler kesinleşmiş kayıtlarındaki bedelle kalır.

İş artışı oranı yalnızca gösterilir; mevzuat sınırı (yüzde) kodda sabit değildir.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.db.repositories import contract_changes as repo
from yapimmaliyet.db.repositories import progress
from yapimmaliyet.db.repositories.contract_changes import Change
from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require, validate_iso_date

CHANGE_STATUSES = {"TASLAK": "Onay bekliyor", "ONAYLI": "Onaylı"}
CHANGE_KINDS = {"IS_ARTISI": "İş artışı", "IS_EKSILISI": "İş eksilişi", "SURE_UZATIMI": "Süre uzatımı",
                "EK_PROTOKOL": "Ek protokol (bedel ve/veya süre)", "DIGER": "Diğer (bedel/süre etkisi yok)"}
ZERO = Decimal(0)


@dataclass(frozen=True)
class ContractSummary:
    contract: progress.Contract
    changes: list[Change]
    increase: Decimal               # iş artışları ve artı yönlü ek protokoller
    decrease: Decimal               # iş eksilişleri ve eksi yönlü ek protokoller (pozitif sayı)
    days: int

    @property
    def pending(self) -> list[Change]:
        return [c for c in self.changes if not c.approved]

    @property
    def amount_delta(self) -> Decimal:
        return self.increase - self.decrease

    @property
    def current_amount(self) -> Decimal:
        return self.contract.contract_amount + self.amount_delta

    @property
    def increase_ratio(self) -> Decimal | None:
        """Artışların asıl sözleşme bedeline oranı (eksilişler düşülmeden)."""
        a = self.contract.contract_amount
        return self.increase / a if a else None

    @property
    def current_end(self) -> str | None:
        end = self.contract.end_date
        if not end or not self.days:
            return end
        return (date.fromisoformat(end) + timedelta(days=self.days)).isoformat()


def summarize(contract: progress.Contract, changes: list[Change]) -> ContractSummary:
    """Toplamlar yalnızca onaylı kayıtlardan; `changes` onay bekleyenler dahil bütün kayıtlardır."""
    approved = [c for c in changes if c.approved]
    deltas = [c.amount_delta for c in approved if c.amount_delta is not None]
    return ContractSummary(contract, changes, sum((d for d in deltas if d > 0), ZERO),
                           -sum((d for d in deltas if d < 0), ZERO), sum(c.days_delta or 0 for c in approved))


def ensure_not_final(conn: sqlite3.Connection, contract_id: int) -> None:
    """Kesin hesabı kesinleşmiş sözleşmede hakediş ve değişiklik kaydı açılamaz/değiştirilemez."""
    if conn.execute("SELECT 1 FROM fa_final_accounts WHERE contract_id = ? AND status = 'KESIN'",
                    (contract_id,)).fetchone():
        raise ConflictError("Bu sözleşmenin kesin hesabı kesinleşmiş; hakediş ve değişiklik kayıtları artık "
                            "değiştirilemez.")


def contract_summary(conn: sqlite3.Connection, contract_id: int) -> ContractSummary | None:
    c = progress.get_contract(conn, contract_id)
    return None if c is None else summarize(c, repo.list_changes(conn, contract_id))


class ContractService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def summaries(self, project_id: int | None = None) -> list[ContractSummary]:
        """Bütün hakediş sözleşmeleri (iki tür); proje verilirse o projenin ve projesiz sözleşmeler."""
        rows = progress.list_contracts(self.conn, "IDARI") + progress.list_contracts(self.conn, "TASERON")
        rows = [c for c in rows if project_id is None or c.project_id in (None, project_id)]
        return [summarize(c, repo.list_changes(self.conn, c.id)) for c in rows]

    def summary(self, contract_id: int) -> ContractSummary:
        s = contract_summary(self.conn, contract_id)
        if s is None:
            raise NotFoundError("Sözleşme bulunamadı.")
        return s

    def changes(self, contract_id: int) -> list[Change]:
        return repo.list_changes(self.conn, contract_id)

    def _data(self, data: dict) -> dict:
        kind = data.get("kind")
        if kind not in CHANGE_KINDS:
            raise ValidationError("Değişiklik türü seçilmelidir.")
        status = data.get("status") or "ONAYLI"
        if status not in CHANGE_STATUSES:
            raise ValidationError("Geçersiz onay durumu.")
        d = {"kind": kind, "status": status, "title": require(data.get("title"), "Değişikliğin konusu"),
             "approval_date": validate_iso_date(data.get("approval_date"), "Onay tarihi"),
             "approval_ref": clean(data.get("approval_ref")), "notes": clean(data.get("notes"))}
        if status == "ONAYLI" and not d["approval_date"] and not d["approval_ref"]:
            raise ValidationError("Onaylı değişiklikte onay tarihi ya da onay sayısı (olur / ek protokol no) "
                                  "girilmelidir; onay bekleyen değişikliği 'Onay bekliyor' olarak kaydedin.")
        amount, days = data.get("amount_delta"), data.get("days_delta")
        try:
            amount = None if amount in (None, "") else to_decimal(amount)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Tutar geçerli bir sayı değil: {amount}") from exc
        try:
            days = int(days) if days not in (None, "", 0) else None
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Gün sayısı tam sayı olmalıdır: {days}") from exc
        if kind in ("IS_ARTISI", "IS_EKSILISI"):
            if amount is None or amount <= 0:
                raise ValidationError(f"{CHANGE_KINDS[kind]} tutarı sıfırdan büyük girilmelidir.")
            amount = amount if kind == "IS_ARTISI" else -amount
        elif kind == "SURE_UZATIMI":
            if days is None or days <= 0:
                raise ValidationError("Süre uzatımı gün sayısı sıfırdan büyük girilmelidir.")
            if amount:
                raise ValidationError("Süre uzatımı kaydında tutar girilmez; bedel değişikliği için ek protokol "
                                      "kullanın.")
            amount = None
        elif kind == "EK_PROTOKOL":
            if not amount and not days:
                raise ValidationError("Ek protokolde tutar farkı ya da gün farkı girilmelidir.")
        elif amount or days:
            raise ValidationError("'Diğer' türündeki kayıt bedeli ve süreyi etkilemez; tutar ve gün boş kalmalıdır.")
        if kind in ("IS_ARTISI", "IS_EKSILISI") and days:
            raise ValidationError("Süre değişikliği için ayrı süre uzatımı kaydı ya da ek protokol girin.")
        d["amount_delta"] = amount or None
        d["days_delta"] = days
        return d

    def add_change(self, contract_id: int, data: dict) -> int:
        c = self.summary(contract_id)
        ensure_not_final(self.conn, contract_id)
        d = self._data(data)
        self._check_result(c, d, None)
        d["change_no"] = repo.next_no(self.conn, contract_id)
        with transaction(self.conn):
            cid = repo.insert_change(self.conn, contract_id, d)
            audit.log(self.conn, "pp_contract_change", cid, "EKLE",
                      new={"contract": contract_id, "kind": d["kind"], "amount": d["amount_delta"],
                           "days": d["days_delta"], "status": d["status"]})
        return cid

    def update_change(self, change_id: int, data: dict) -> None:
        authz.require("SOZLESME", authz.APPROVE, "sözleşme değişikliği onayı")
        old = repo.get_change(self.conn, change_id)
        if old is None:
            raise NotFoundError("Değişiklik kaydı bulunamadı.")
        ensure_not_final(self.conn, old.contract_id)
        d = self._data(data)
        self._check_result(self.summary(old.contract_id), d, change_id)
        d["change_no"] = old.change_no
        with transaction(self.conn):
            repo.update_change(self.conn, change_id, d)
            audit.log(self.conn, "pp_contract_change", change_id, "GUNCELLE",
                      old={"amount": old.amount_delta, "days": old.days_delta, "status": old.status},
                      new={"amount": d["amount_delta"], "days": d["days_delta"], "status": d["status"]})

    def approve_change(self, change_id: int, approval_date: str | None, approval_ref: str | None) -> None:
        """Onay bekleyen kaydı onaylar; onaylı kayıt ikinci kez onaylanamaz (tutar bir kez yansır)."""
        old = repo.get_change(self.conn, change_id)
        if old is None:
            raise NotFoundError("Değişiklik kaydı bulunamadı.")
        if old.approved:
            raise ConflictError("Bu değişiklik zaten onaylı.")
        data = {f: getattr(old, f) for f in ("kind", "title", "amount_delta", "days_delta", "notes")}
        if old.kind == "IS_EKSILISI" and old.amount_delta is not None:
            data["amount_delta"] = -old.amount_delta
        self.update_change(change_id, {**data, "status": "ONAYLI", "approval_date": approval_date or old.approval_date,
                                       "approval_ref": approval_ref or old.approval_ref})

    def changes_from_comparison(self, contract_id: int, comparison_id: int) -> list[int]:
        """Mukayeseli keşif sonucunu 'Onay bekliyor' durumunda iş artışı ve iş eksilişi kayıtları olarak aktarır
        (artışlar ve eksilişler ayrı kayıt; tutarlar karşılaştırma satırlarının tutar farklarından). Onay bilgisi
        girilip onaylanana kadar güncel bedele yansımaz. Aynı karşılaştırma ikinci kez aktarılamaz."""
        from yapimmaliyet.services.estimate_comparison_service import EstimateComparisonService

        s = self.summary(contract_id)
        cmp_svc = EstimateComparisonService(self.conn)
        info = cmp_svc.get(comparison_id)
        marker = f"[mukayese:{comparison_id}]"
        if any(marker in (c.notes or "") for c in s.changes):
            raise ConflictError(f"'{info.name}' karşılaştırması bu sözleşmeye zaten aktarılmış.")
        projects = {r[0] for r in self.conn.execute(
            "SELECT project_id FROM estimates WHERE id IN (?, ?)", (info.first_estimate_id, info.second_estimate_id))}
        if s.contract.project_id is not None and s.contract.project_id not in projects:
            raise ValidationError("Karşılaştırmanın keşifleri bu sözleşmenin projesine ait değil.")
        result = cmp_svc.compute(comparison_id)
        if result.summary.unresolved:
            raise ValidationError(f"Karşılaştırmada fiyatı eksik ya da çözülmemiş {result.summary.unresolved} satır var; "
                                  "önce Mukayeseli Keşif sayfasında giderin.")
        diffs = [r.amount_diff for r in result.rows if not r.unresolved and r.amount_diff]
        increase = sum((d for d in diffs if d > 0), ZERO)
        decrease = -sum((d for d in diffs if d < 0), ZERO)
        if not increase and not decrease:
            raise ValidationError("Karşılaştırmada tutar farkı yok; aktarılacak değişiklik bulunmadı.")
        note = (f"{marker} Kaynak: Mukayeseli Keşif '{info.name}' (artış {increase}, eksiliş {decrease}, "
                f"net {increase - decrease}). Onay tarihi / sayısı girilip onaylanınca güncel bedele yansır.")
        ids = []
        for kind, amount, label in (("IS_ARTISI", increase, "artan ve yeni kalemler"),
                                    ("IS_EKSILISI", decrease, "azalan ve çıkan kalemler")):
            if amount:
                ids.append(self.add_change(contract_id, {"kind": kind, "status": "TASLAK", "amount_delta": amount,
                                                         "title": f"{info.name}: {label}", "notes": note}))
        return ids

    def delete_change(self, change_id: int) -> None:
        old = repo.get_change(self.conn, change_id)
        if old is None:
            raise NotFoundError("Değişiklik kaydı bulunamadı.")
        ensure_not_final(self.conn, old.contract_id)
        rest = [c for c in self.changes(old.contract_id) if c.id != change_id]
        if summarize(progress.get_contract(self.conn, old.contract_id), rest).current_amount < 0:
            raise ValidationError("Bu kayıt silinirse güncel sözleşme bedeli negatif olur.")
        with transaction(self.conn):
            repo.delete_change(self.conn, change_id)
            audit.log(self.conn, "pp_contract_change", change_id, "SIL",
                      old={"contract": old.contract_id, "no": old.change_no, "title": old.title})

    def _check_result(self, s: ContractSummary, d: dict, replacing: int | None) -> None:
        rest = [c for c in s.changes if c.id != replacing and c.approved]
        pending = d["status"] != "ONAYLI"
        delta = sum((c.amount_delta or ZERO for c in rest), ZERO) + (ZERO if pending else d["amount_delta"] or ZERO)
        if s.contract.contract_amount + delta < 0:
            raise ValidationError("Değişikliklerden sonra güncel sözleşme bedeli negatif olamaz.")
