"""Disiplinler, ihale (teklif) modelleri ve ihale tekliflerinin yaklaşık maliyetle karşılaştırılması.

Saf hesap: veritabanı ve arayüz bilmez. Tutarlar Decimal; satır tutarı = miktar × birim fiyat (2 basamak, HALF_UP).
Modeller:
- BIRIM_FIYAT: her kalem için teklif birim fiyatı; teklif tutarı Σ miktar × birim fiyat.
- ANAHTAR_TESLIM: işin tamamı için tek götürü tutar; kalem fiyatı sözleşme bedelini oluşturmaz.
- KARMA: her kalemin yöntemi ayrıca tutulur; birim fiyatlı kalemlerde birim fiyat, götürü kalemlerde kalem tutarı.
Sıralama yalnızca bilgi amaçlıdır; aşırı düşük teklif sınır değeri gibi mevzuat hesapları burada yapılmaz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.rounding import DEFAULT_POLICY, line_amount, quantize, total

DISCIPLINES = {"INSAAT": "İnşaat", "ELEKTRIK": "Elektrik", "MEKANIK": "Mekanik tesisat", "ALTYAPI": "Altyapı"}
TENDER_MODELS = {"BIRIM_FIYAT": "Birim fiyatlı", "ANAHTAR_TESLIM": "Anahtar teslim götürü bedel",
                 "KARMA": "Karma (birim fiyat + götürü bedel)"}
PRICING_METHODS = {"BIRIM_FIYAT": "Birim fiyat", "GOTURU": "Götürü bedel"}
UNASSIGNED = "Atanmamış"

RANK_NOTE = ("Sıralama ve oranlar yalnızca bilgi amaçlıdır. İhale kararı, aşırı düşük teklif sorgulaması ve "
             "değerlendirme mevzuata göre ihale komisyonunca yapılır; sınır değer hesabı bu programda yapılmaz.")


def parse_disciplines(text: str | None) -> tuple[str, ...]:
    codes = tuple(c.strip() for c in (text or "").split(",") if c.strip())
    bad = [c for c in codes if c not in DISCIPLINES]
    if bad:
        raise ValueError(f"Geçersiz disiplin: {', '.join(bad)}")
    return codes


def join_disciplines(codes) -> str:
    ordered = [c for c in DISCIPLINES if c in set(codes)]
    if len(ordered) != len(set(codes)):
        raise ValueError("Geçersiz disiplin kodu.")
    return ",".join(ordered)


def effective_method(model: str, item_method: str | None) -> str | None:
    """Kalemin teklif yöntemi: birim fiyatlı ve anahtar teslim modelde modelden, karma modelde kalemden gelir."""
    if model == "BIRIM_FIYAT":
        return "BIRIM_FIYAT"
    if model == "ANAHTAR_TESLIM":
        return "GOTURU"
    if model == "KARMA":
        return item_method
    raise ValueError(f"Geçersiz ihale modeli: {model}")


@dataclass(frozen=True)
class TenderLine:
    item_id: int
    line_no: int
    code: str | None
    description: str | None
    unit: str | None
    quantity: Decimal
    discipline: str | None
    method: str | None            # etkin yöntem (effective_method)
    ym_unit_price: Decimal | None
    ym_amount: Decimal | None


@dataclass(frozen=True)
class PartTotal:
    key: str
    label: str
    count: int
    amount: Decimal
    unpriced: int


@dataclass(frozen=True)
class TenderSummary:
    model: str
    by_discipline: tuple[PartTotal, ...]
    by_method: tuple[PartTotal, ...]
    unassigned_discipline: int
    unassigned_method: int
    notes: tuple[str, ...]

    @property
    def model_label(self) -> str:
        return TENDER_MODELS[self.model]


def _parts(lines: list[TenderLine], key, labels: dict[str, str]) -> tuple[PartTotal, ...]:
    groups: dict[str, list[TenderLine]] = {}
    for ln in lines:
        groups.setdefault(key(ln) or "", []).append(ln)
    order = [k for k in labels if k in groups] + ([""] if "" in groups else [])
    return tuple(PartTotal(k or "-", labels.get(k, UNASSIGNED), len(groups[k]),
                           total([x.ym_amount for x in groups[k] if x.ym_amount is not None]),
                           sum(x.ym_amount is None for x in groups[k])) for k in order)


def summarize(model: str, lines: list[TenderLine]) -> TenderSummary:
    """Yaklaşık maliyetin disipline ve teklif yöntemine göre dökümü (ihale modeli bilgisiyle)."""
    if model not in TENDER_MODELS:
        raise ValueError(f"Geçersiz ihale modeli: {model}")
    no_disc = sum(ln.discipline is None for ln in lines)
    no_method = sum(ln.method is None for ln in lines)
    notes = [f"İhale modeli: {TENDER_MODELS[model]}."]
    if model == "ANAHTAR_TESLIM":
        notes.append("Anahtar teslim götürü bedelde teklif işin tamamı için tek tutardır; kalem miktarları "
                     "yaklaşık maliyetin dayanağıdır, sözleşme bedelini kalem bazında oluşturmaz.")
    elif model == "KARMA":
        notes.append("Karma modelde birim fiyatlı ve götürü bedelli kısımlar ayrı toplanır.")
        if no_method:
            notes.append(f"UYARI: {no_method} kalemin teklif yöntemi (birim fiyat / götürü) atanmamış; "
                         "teklif karşılaştırması bu kalemler atanmadan yapılamaz.")
    if no_disc:
        notes.append(f"{no_disc} kalemin disiplini atanmamış ('{UNASSIGNED}' grubunda gösterilir).")
    return TenderSummary(model, _parts(lines, lambda x: x.discipline, DISCIPLINES),
                         _parts(lines, lambda x: x.method, PRICING_METHODS), no_disc, no_method, tuple(notes))


@dataclass(frozen=True)
class BidLine:
    item_id: int
    unit_price: Decimal | None = None
    lump_sum: Decimal | None = None


@dataclass(frozen=True)
class Bid:
    id: int
    bidder: str
    lump_total: Decimal | None = None
    lines: tuple[BidLine, ...] = ()
    is_sample: bool = False


@dataclass(frozen=True)
class BidEvaluation:
    bid: Bid
    total: Decimal | None              # eksik kalem varsa yalnız girilenlerin toplamı (complete=False)
    complete: bool
    missing: tuple[int, ...]           # eksik/uyumsuz kalemlerin sıra numaraları
    amounts: dict[int, Decimal | None] = field(default_factory=dict)   # kalem → teklif tutarı
    diff: Decimal | None = None        # teklif − yaklaşık maliyet
    ratio: Decimal | None = None       # teklif / yaklaşık maliyet (4 basamak)
    rank: int | None = None            # tam teklifler arasında artan tutar sırası (bilgi)


@dataclass(frozen=True)
class BidComparison:
    model: str
    ym_total: Decimal | None
    rows: tuple[BidEvaluation, ...]
    notes: tuple[str, ...]


def _evaluate(model: str, lines: list[TenderLine], bid: Bid) -> tuple[Decimal | None, bool, tuple, dict]:
    if model == "ANAHTAR_TESLIM":
        return bid.lump_total, bid.lump_total is not None, (), {}
    given = {b.item_id: b for b in bid.lines}
    amounts: dict[int, Decimal | None] = {}
    missing = []
    for ln in lines:
        b = given.get(ln.item_id)
        amt = None
        if b is not None and ln.method == "BIRIM_FIYAT" and b.unit_price is not None:
            amt = line_amount(ln.quantity, b.unit_price, DEFAULT_POLICY)
        elif b is not None and ln.method == "GOTURU" and b.lump_sum is not None:
            amt = quantize(b.lump_sum, DEFAULT_POLICY.amount_places, DEFAULT_POLICY.mode)
        amounts[ln.item_id] = amt
        if amt is None:
            missing.append(ln.line_no)
    known = [a for a in amounts.values() if a is not None]
    return (total(known) if known else None), not missing and bool(lines), tuple(missing), amounts


def compare_bids(model: str, lines: list[TenderLine], ym_total: Decimal | None, bids: list[Bid]) -> BidComparison:
    if model not in TENDER_MODELS:
        raise ValueError(f"Geçersiz ihale modeli: {model}")
    notes = [f"İhale modeli: {TENDER_MODELS[model]}.", RANK_NOTE]
    if model == "KARMA" and any(ln.method is None for ln in lines):
        notes.append("Teklif yöntemi atanmamış kalemler var; bu kalemler her teklifte eksik sayılır.")
    if ym_total is None:
        notes.append("Yaklaşık maliyet toplamı yok (eksik fiyatlı kalemler olabilir); oran hesaplanmadı.")
    evaluated = []
    for bid in bids:
        tot, complete, missing, amounts = _evaluate(model, lines, bid)
        diff = ratio = None
        if complete and tot is not None and ym_total:
            diff = tot - ym_total
            ratio = quantize(tot / ym_total, 4)
        evaluated.append(BidEvaluation(bid, tot, complete, missing, amounts, diff, ratio))
    ordered = sorted({e.total for e in evaluated if e.complete and e.total is not None})
    rows = tuple(BidEvaluation(e.bid, e.total, e.complete, e.missing, e.amounts, e.diff, e.ratio,
                               ordered.index(e.total) + 1 if e.complete and e.total is not None else None)
                 for e in evaluated)
    incomplete = [e.bid.bidder for e in rows if not e.complete]
    if incomplete:
        notes.append(f"Eksik teklif (sıralamaya alınmadı): {', '.join(incomplete)}.")
    return BidComparison(model, ym_total, rows, tuple(notes))
