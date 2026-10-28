"""Hesap motoru: referans fiyat (A) ve idare esaslı tahmini yaklaşık maliyet (B).

Motor saf Python'dur; veritabanı, dosya ve arayüz bilmez. Girdiler değişmez veri sınıflarıdır.
Mevzuat kuralları koda gömülmez; profil parametresi olarak gelir ve sonuç notlarında gösterilir.
Firma maliyeti (C) ve teklif senaryoları (D) Aşama 6'da eklenecektir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from enum import StrEnum

from yapimmaliyet.domain.indexing import SOURCE_NOTE, IndexRatio, apply_factor
from yapimmaliyet.domain.money import to_decimal
from yapimmaliyet.domain.rounding import RoundingPolicy, line_amount, quantize, total

DISCLAIMER = ("Bu hesap teklif hazırlık amaçlı bir tahmindir; idarenin resmî yaklaşık maliyeti değildir. "
              "Sonuçlar ihale dokümanına ve yürürlükteki mevzuata göre kontrol edilmelidir.")


class CalcKind(StrEnum):
    REFERANS = "REFERANS"
    YAKLASIK = "YAKLASIK"

    @property
    def label(self) -> str:
        return {"REFERANS": "Referans fiyat hesabı", "YAKLASIK": "İdare esaslı tahmini yaklaşık maliyet"}[self.value]


class PriceOrigin(StrEnum):
    KITAP = "KITAP"
    OZEL_DOGRUDAN = "OZEL_DOGRUDAN"
    OZEL_ANALIZ = "OZEL_ANALIZ"
    YOK = "YOK"

    @property
    def label(self) -> str:
        return {"KITAP": "Birim fiyat kitabı", "OZEL_DOGRUDAN": "Özel poz (doğrudan fiyat)",
                "OZEL_ANALIZ": "Özel poz (analiz)", "YOK": "Fiyat yok"}[self.value]


@dataclass(frozen=True)
class ProfileParams:
    rounding: RoundingPolicy = RoundingPolicy()
    prices_include_overhead_profit: bool = True
    overhead_profit_rate: Decimal = Decimal(0)
    vat_rate: Decimal | None = None
    index_update: bool = False   # güncelliğini yitirmiş fiyatlar endeksle güncellenir (YİİUY md.10)

    @classmethod
    def from_json(cls, data: dict) -> "ProfileParams":
        if not isinstance(data, dict):
            raise ValueError("Profil parametreleri bir nesne olmalıdır.")
        r = data.get("rounding", {})
        mode = {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}.get(r.get("mode", "HALF_UP"))
        if mode is None:
            raise ValueError("Yuvarlama yöntemi HALF_UP ya da HALF_EVEN olmalıdır.")
        try:
            policy = RoundingPolicy(int(r.get("quantity_places", 3)), int(r.get("price_places", 2)),
                                    int(r.get("amount_places", 2)), int(r.get("analysis_places", 6)), mode)
            rate = to_decimal(str(data.get("overhead_profit_rate", "0")))
            vat = data.get("vat_rate")
            vat_d = None if vat in (None, "") else to_decimal(str(vat))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Profil parametresi geçersiz: {exc}") from exc
        if not 0 <= rate <= 1:
            raise ValueError("Kâr ve genel gider oranı %0 ile %100 arasında olmalıdır.")
        if vat_d is not None and not 0 <= vat_d <= 1:
            raise ValueError("KDV oranı %0 ile %100 arasında olmalıdır.")
        include = data.get("prices_include_overhead_profit", True)
        index_update = data.get("index_update", False)
        if not isinstance(include, bool) or not isinstance(index_update, bool):
            raise ValueError("'prices_include_overhead_profit' ve 'index_update' doğru/yanlış olmalıdır.")
        return cls(policy, include, rate, vat_d, index_update)

    def to_json(self) -> dict:
        p = self.rounding
        return {
            "rounding": {"quantity_places": p.quantity_places, "price_places": p.price_places,
                         "amount_places": p.amount_places, "analysis_places": p.analysis_places,
                         "mode": "HALF_UP" if p.mode == ROUND_HALF_UP else "HALF_EVEN"},
            "prices_include_overhead_profit": self.prices_include_overhead_profit,
            "overhead_profit_rate": str(self.overhead_profit_rate),
            "vat_rate": None if self.vat_rate is None else str(self.vat_rate),
            "index_update": self.index_update,
        }


@dataclass(frozen=True)
class CalcInput:
    item_id: int
    line_no: int
    work_group: str | None
    code: str | None
    description: str | None
    unit: str | None
    quantity: Decimal
    matched_price: Decimal | None
    origin: PriceOrigin
    price_source: str
    price_date: str | None
    user_override: Decimal | None = None
    match_status: str = "ONAYLANDI"
    unit_mismatch: bool = False
    unit_mismatch_ack: bool = False
    index_ratio: IndexRatio | None = None   # endeks güncellemesi (güncel / temel)
    index_problem: str | None = None        # güncelleme gerekli ama yapılamıyor (eksik endeks/tarih)
    discipline: str | None = None           # INSAAT | ELEKTRIK | MEKANIK | ALTYAPI (hesabı etkilemez; döküm için)
    pricing_method: str | None = None       # karma modelde BIRIM_FIYAT | GOTURU (hesabı etkilemez; döküm için)
    price_authority: str | None = None      # fiyatı yayımlayan kurum (rapor başlığındaki fiyat kaynağı)
    price_period: str | None = None         # fiyat dönemi ("2026 Ocak"); bilinmiyorsa None


@dataclass(frozen=True)
class CalcLine:
    input: CalcInput
    unit_price: Decimal | None
    amount: Decimal | None
    overridden: bool
    problem: str | None
    base_price: Decimal | None = None       # endeksle güncellenmeden önceki fiyat

    @property
    def index_updated(self) -> bool:
        return self.base_price is not None


@dataclass(frozen=True)
class GroupTotal:
    name: str
    count: int
    amount: Decimal


@dataclass(frozen=True)
class CalcResult:
    kind: CalcKind
    lines: tuple[CalcLine, ...]
    groups: tuple[GroupTotal, ...]
    subtotal: Decimal
    overhead_profit: Decimal
    total_excl_vat: Decimal
    vat: Decimal | None
    total_incl_vat: Decimal | None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def finalizable(self) -> bool:
        return not self.problems


def _line(inp: CalcInput, policy: RoundingPolicy) -> CalcLine:
    overridden = inp.user_override is not None
    price = inp.user_override if overridden else inp.matched_price
    problem = None
    if inp.match_status != "ONAYLANDI":
        problem = {"ESLESMEDI": "Eşleşmemiş", "ONERILDI": "Eşleşme önerisi onaylanmadı",
                   "ERTELENDI": "Eşleştirme ertelendi"}.get(inp.match_status, inp.match_status)
    elif inp.unit_mismatch and not inp.unit_mismatch_ack:
        problem = "Birim uyuşmazlığı onaylanmadı"
    if price is None:
        return CalcLine(inp, None, None, overridden, problem or "Fiyat yok")
    base = None
    if not overridden:   # kullanıcının elle girdiği fiyat olduğu gibi kullanılır
        if inp.index_problem:
            problem = problem or inp.index_problem
        elif inp.index_ratio is not None:
            base, price = price, apply_factor(price, inp.index_ratio.factor, policy.price_places, policy.mode)
    return CalcLine(inp, price, line_amount(inp.quantity, price, policy), overridden, problem, base)


def calculate(kind: CalcKind, inputs: list[CalcInput], params: ProfileParams,
              extra_problems: tuple[str, ...] = ()) -> CalcResult:
    policy = params.rounding
    lines = tuple(_line(i, policy) for i in sorted(inputs, key=lambda x: x.line_no))
    groups: dict[str, list[CalcLine]] = {}
    for ln in lines:
        groups.setdefault(ln.input.work_group or "Grupsuz", []).append(ln)
    group_totals = tuple(GroupTotal(name, len(ls), total([x.amount for x in ls if x.amount is not None], policy))
                         for name, ls in groups.items())
    subtotal = total([ln.amount for ln in lines if ln.amount is not None], policy)
    problems = list(extra_problems) + [
        f"Sıra {ln.input.line_no} ({ln.input.code or ln.input.description or '-'}): {ln.problem}"
        for ln in lines if ln.problem]
    notes: list[str] = []
    unpriced = [ln for ln in lines if ln.amount is None]
    if unpriced:   # eksik fiyat sıfır sayılmaz ve toplam "tam maliyet" gibi sunulmaz
        notes.append(f"EKSİK FİYATLI ÖN TOPLAM: {len(unpriced)} satırın birim fiyatı yok; bu satırlar toplama dahil "
                     "değildir. Eksikler tamamlanmadan bu tutar tam/kesin maliyet değildir.")
    if params.index_update and kind is CalcKind.YAKLASIK:
        n = sum(ln.index_updated for ln in lines)
        notes.append(f"Endeks güncellemesi: {n} satırda birim fiyat × (güncel endeks / temel endeks) uygulandı; "
                     "hesap tarihi itibarıyla güncel fiyatlar değiştirilmedi. " + SOURCE_NOTE)
    overhead_profit = Decimal("0.00")
    vat = total_incl = None
    if kind is CalcKind.REFERANS:
        notes.append("Referans fiyat hesabıdır: seçilen fiyatlarla keşif toplamı; gider, kâr ya da vergi eklenmez.")
        total_excl = subtotal
    else:
        if params.prices_include_overhead_profit:
            notes.append("Profil varsayımı: birim fiyatlar yüklenici kârı ve genel gideri içerir; ayrıca eklenmedi.")
        else:
            overhead_profit = quantize(subtotal * params.overhead_profit_rate, policy.amount_places, policy.mode)
            notes.append(f"Profil varsayımı: birim fiyatlara %{params.overhead_profit_rate * 100:.2f} kâr ve genel "
                         "gider eklendi.".replace(".", ",", 1))
        total_excl = subtotal + overhead_profit
        if params.vat_rate is not None:
            vat = quantize(total_excl * params.vat_rate, policy.amount_places, policy.mode)
            total_incl = total_excl + vat
            notes.append(f"KDV yalnızca bilgi amaçlıdır (%{params.vat_rate * 100:.0f}); yaklaşık maliyet KDV hariç toplamdır.")
        if any(ln.input.origin in (PriceOrigin.OZEL_DOGRUDAN, PriceOrigin.OZEL_ANALIZ) for ln in lines):
            notes.append("Özel poz fiyatları kullanıcı tarafından oluşturulmuştur; kâr ve genel gider içerip "
                         "içermedikleri ayrıca kontrol edilmelidir.")
        notes.append(DISCLAIMER)
    if any(ln.overridden for ln in lines):
        notes.append(f"{sum(ln.overridden for ln in lines)} satırda birim fiyat kullanıcı tarafından değiştirildi.")
    return CalcResult(kind, lines, group_totals, subtotal, overhead_profit, total_excl, vat, total_incl, problems, notes)
