"""İhale modeli dökümü (disiplin / teklif yöntemi) ve ihale tekliflerinin yaklaşık maliyetle karşılaştırılması."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, cost_sheets, estimate_items, parties, projects, tender_bids
from yapimmaliyet.db.repositories.tender_bids import BidRow
from yapimmaliyet.domain.calc import CalcResult
from yapimmaliyet.domain.money import decimal_places
from yapimmaliyet.domain.tender import (
    Bid,
    BidComparison,
    BidLine,
    TenderLine,
    TenderSummary,
    compare_bids,
    effective_method,
    summarize,
)
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.calc_service import CalcService
from yapimmaliyet.services.offer_service import OfferService
from yapimmaliyet.services.validators import clean, require, validate_iso_date, validate_price


@dataclass(frozen=True)
class TenderView:
    """Bir keşfin ihale modeli bakışı: proje, hesap sonucu, ihale satırları ve döküm."""
    project: object
    estimate: object
    result: CalcResult
    lines: list[TenderLine]
    summary: TenderSummary

    @property
    def ym_total(self) -> Decimal | None:
        """Karşılaştırma tabanı: KDV hariç yaklaşık maliyet; fiyatsız kalem varsa None (eksik toplam taban olmaz)."""
        if any(ln.amount is None for ln in self.result.lines):
            return None
        return self.result.total_excl_vat


def _amount(value, label: str) -> Decimal:
    d = validate_price(value)
    if decimal_places(d) > 2:
        raise ValidationError(f"{label} en fazla 2 ondalık basamak içerebilir: {d}")
    return d


class TenderService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.calc = CalcService(conn)

    def default_profile(self) -> int:
        profs = [p for p in self.calc.profiles() if p.kind == "YAKLASIK"] or self.calc.profiles()
        if not profs:
            raise ValidationError("Hesap profili bulunamadı.")
        return profs[0].id

    def view(self, estimate_id: int, profile_id: int | None = None) -> TenderView:
        est = projects.get_estimate(self.conn, estimate_id)
        if est is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        project = projects.get_project(self.conn, est.project_id)
        result = self.calc.calculate(estimate_id, profile_id or self.default_profile())
        items = {it.id: it for it in estimate_items.list_items(self.conn, estimate_id)}
        lines = []
        for ln in result.lines:
            it = items[ln.input.item_id]
            lines.append(TenderLine(it.id, it.line_no, ln.input.code, ln.input.description, ln.input.unit,
                                    ln.input.quantity, it.discipline,
                                    effective_method(project.tender_model, it.pricing_method), ln.unit_price,
                                    ln.amount))
        return TenderView(project, est, result, lines, summarize(project.tender_model, lines))

    # --- teklifler -----------------------------------------------------------
    def list_bids(self, estimate_id: int) -> list[BidRow]:
        return tender_bids.list_bids(self.conn, estimate_id)

    def bid_lines(self, bid_id: int) -> dict[int, tuple[Decimal | None, Decimal | None]]:
        return tender_bids.lines(self.conn, bid_id)

    def _validated(self, estimate_id: int, lump_total, lines: dict[int, Decimal]):
        est = projects.get_estimate(self.conn, estimate_id)
        if est is None:
            raise NotFoundError("Keşif listesi bulunamadı.")
        model = projects.get_project(self.conn, est.project_id).tender_model
        items = {it.id: it for it in estimate_items.list_items(self.conn, estimate_id)}
        if model == "ANAHTAR_TESLIM":
            if lump_total is None:
                raise ValidationError("Anahtar teslim götürü bedel teklifinde teklif tutarı girilmelidir.")
            if lines:
                raise ValidationError("Anahtar teslim götürü bedel teklifi kalem fiyatı içermez; tek tutar girilir.")
            return _amount(lump_total, "Teklif tutarı"), []
        if lump_total is not None:
            raise ValidationError("Birim fiyatlı ve karma tekliflerde toplam, kalem fiyatlarından hesaplanır.")
        rows = []
        for item_id, value in lines.items():
            it = items.get(item_id)
            if it is None:
                raise ValidationError("Teklif satırı bu keşfe ait olmayan bir kaleme bağlı.")
            method = effective_method(model, it.pricing_method)
            if method is None:
                raise ValidationError(f"Sıra {it.line_no}: kalemin teklif yöntemi (birim fiyat / götürü) atanmamış.")
            if method == "BIRIM_FIYAT":
                rows.append((item_id, validate_price(value), None))
            else:
                rows.append((item_id, None, _amount(value, f"Sıra {it.line_no} götürü tutarı")))
        return None, rows

    def create_bid(self, estimate_id: int, bidder: str, bid_date: str | None = None, lump_total=None,
                   lines: dict[int, Decimal] | None = None, is_sample: bool = False, notes: str | None = None) -> int:
        """Teklif kaydı. Birim fiyatlı kalemde `lines` değeri birim fiyat, götürü kalemde kalem tutarıdır."""
        name = require(bidder, "İstekli adı")
        bid_date = validate_iso_date(bid_date, "Teklif tarihi")
        total, rows = self._validated(estimate_id, lump_total, lines or {})
        if any(b.bidder == name for b in self.list_bids(estimate_id)):
            raise ConflictError(f"'{name}' için bu keşifte zaten teklif var.")
        with transaction(self.conn):
            bid = tender_bids.insert_bid(self.conn, estimate_id, name, bid_date, total, is_sample, clean(notes))
            tender_bids.replace_lines(self.conn, bid, rows)
            parties.link_unlinked(self.conn)
            audit.log(self.conn, "tender_bid", bid, "EKLE", new={"bidder": name, "lines": len(rows),
                                                                  "lump_total": total})
        return bid

    def update_bid(self, bid_id: int, bidder: str, bid_date: str | None, lump_total=None,
                   lines: dict[int, Decimal] | None = None, notes: str | None = None) -> None:
        old = tender_bids.get_bid(self.conn, bid_id)
        if old is None:
            raise NotFoundError("Teklif bulunamadı.")
        name = require(bidder, "İstekli adı")
        total, rows = self._validated(old.estimate_id, lump_total, lines or {})
        if any(b.bidder == name and b.id != bid_id for b in self.list_bids(old.estimate_id)):
            raise ConflictError(f"'{name}' için bu keşifte zaten teklif var.")
        with transaction(self.conn):
            tender_bids.update_bid(self.conn, bid_id, name, validate_iso_date(bid_date, "Teklif tarihi"), total,
                                   clean(notes))
            tender_bids.replace_lines(self.conn, bid_id, rows)
            parties.link_unlinked(self.conn)
            audit.log(self.conn, "tender_bid", bid_id, "GUNCELLE", old={"bidder": old.bidder},
                      new={"bidder": name, "lines": len(rows), "lump_total": total})

    def delete_bid(self, bid_id: int) -> None:
        old = tender_bids.get_bid(self.conn, bid_id)
        if old is None:
            raise NotFoundError("Teklif bulunamadı.")
        with transaction(self.conn):
            tender_bids.delete_bid(self.conn, bid_id)
            audit.log(self.conn, "tender_bid", bid_id, "SIL", old={"bidder": old.bidder})

    def compare(self, estimate_id: int, profile_id: int | None = None) -> tuple[TenderView, BidComparison]:
        view = self.view(estimate_id, profile_id)
        bids = []
        for b in self.list_bids(estimate_id):
            ls = tuple(BidLine(i, up, lump) for i, (up, lump) in self.bid_lines(b.id).items())
            bids.append(Bid(b.id, b.bidder, b.lump_total, ls, b.is_sample))
        return view, compare_bids(view.project.tender_model, view.lines, view.ym_total, bids)

    # --- maliyet çalışmasından teklif ----------------------------------------------
    def bid_from_offer(self, sheet_id: int, scenario_id: int, bidder: str, bid_date: str | None = None) -> tuple[int, Decimal]:
        """Firma maliyet çalışmasındaki teklif senaryosunu teklif kaydına aktarır: birim fiyatlı kalemde senaryonun
        birim fiyatı, götürü kalemde kalem tutarı, anahtar teslimde senaryo toplamı. Aktarımdan sonra teklif toplamı
        senaryo toplamıyla karşılaştırılır; farklıysa kayıt yapılmaz. Döner: (teklif kimliği, toplam)."""
        offers = OfferService(self.conn)
        sheet = cost_sheets.get_sheet(self.conn, sheet_id)
        if sheet is None:
            raise NotFoundError("Maliyet çalışması bulunamadı.")
        ids = [sid for sid, _ in offers.scenarios(sheet_id)]
        if scenario_id not in ids:
            raise NotFoundError("Teklif senaryosu bulunamadı.")
        offer = offers.analyse(sheet_id).offers[ids.index(scenario_id)]
        view = self.view(sheet.estimate_id)
        by_item = {ol.cost.input.item_id: ol for ol in offer.lines}
        if view.project.tender_model == "ANAHTAR_TESLIM":
            lump, lines = offer.offer_total, {}
        else:
            lump, lines = None, {}
            for ln in view.lines:
                ol = by_item.get(ln.item_id)
                if ol is None:
                    continue
                lines[ln.item_id] = ol.unit_price if ln.method == "BIRIM_FIYAT" else ol.amount
        bids = [Bid(0, bidder, lump, tuple(BidLine(i, v if self._method(view, i) == "BIRIM_FIYAT" else None,
                                                   None if self._method(view, i) == "BIRIM_FIYAT" else v)
                                           for i, v in lines.items()))]
        check = compare_bids(view.project.tender_model, view.lines, None, bids).rows[0]
        if not check.complete or check.total != offer.offer_total:
            raise ConflictError(f"Teklif toplamı ({check.total}) senaryo toplamıyla ({offer.offer_total}) uyuşmuyor ya da "
                                "eksik kalem var; teklif oluşturulmadı. Maliyet çalışmasındaki kalemleri kontrol edin.")
        note = (f"Kaynak: maliyet çalışması '{sheet.name}', senaryo '{offer.scenario.name}' "
                f"(kâr %{offer.scenario.profit_rate * 100:.2f}, risk %{offer.scenario.risk_rate * 100:.2f}); "
                f"senaryo toplamı {offer.offer_total} TL ile denetlendi.")
        return self.create_bid(sheet.estimate_id, bidder, bid_date, lump, lines, notes=note), check.total

    @staticmethod
    def _method(view: TenderView, item_id: int) -> str | None:
        return next(ln.method for ln in view.lines if ln.item_id == item_id)
