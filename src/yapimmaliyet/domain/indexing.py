"""Endeksle fiyat güncelleme ve sözleşme sürecinde yeni birim fiyat tespiti kuralları (saf hesap).

Kaynak: hakedis.org, "Yeni Mevzuata Göre Fiyat Tespiti" (16.09.2026). Yazı, 30.07.2025 tarihli ve 32971
sayılı Resmî Gazete'deki değişikliklere (1 Eylül 2025'ten sonra ilanı yapılan ihaleler) dayanır ve
Yapım İşleri İhaleleri Uygulama Yönetmeliği md. 10 ile Yapım İşleri Genel Şartnamesi md. 22'yi aktarır.
Bu bir ikincil kaynaktır; kurallar resmî metinle karşılaştırılarak doğrulanmalıdır. Her sonuç, hangi
dönem ve endeks değerlerinin kullanıldığını adım adım gösterir.

Dönemler "YYYY-AA" biçiminde ay olarak tutulur. Endeks değerleri bu modüle dışarıdan verilir; program
endeks değeri uydurmaz.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from yapimmaliyet.domain.money import CALC_CONTEXT, to_decimal
from yapimmaliyet.domain.rounding import quantize

SOURCE_NOTE = ("Kural kaynağı: hakedis.org \"Yeni Mevzuata Göre Fiyat Tespiti\" (YİİUY md.10, YİGŞ md.22 aktarımı; "
               "1 Eylül 2025 sonrası ihaleler). Resmî metinle doğrulayın.")
GENERAL_SERIES = "YI-UFE"   # alt endeks belirlenemezse kullanılan Yİ-ÜFE genel endeksi
SIMILAR_RATIO_LIMIT = Decimal("0.90")
DEFAULT_PUBLIC_OVERHEAD = Decimal("0.25")   # kamu kurum pozları için %25 kâr ve genel gider


# --- dönemler -------------------------------------------------------------
def period_of(value: date | str) -> str:
    """Tarihin (ya da "YYYY-AA[-GG]" metninin) içinde bulunduğu ay."""
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}"
    text = str(value).strip()
    if len(text) < 7 or text[4] != "-" or not (text[:4] + text[5:7]).isdigit() or not 1 <= int(text[5:7]) <= 12:
        raise ValueError(f"Tarih ay bilgisi içermiyor: {value!r} (YYYY-AA-GG bekleniyor)")
    return text[:7]


def previous_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def period_label(period: str) -> str:
    months = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
              "Kasım", "Aralık")
    return f"{months[int(period[5:7]) - 1]} {period[:4]}"


IndexLookup = Callable[[str, str], Decimal | None]   # (seri kodu, dönem) → değer ya da None


class MissingIndexError(ValueError):
    def __init__(self, series: str, period: str):
        super().__init__(f"Endeks değeri girilmemiş: {series} — {period_label(period)}")
        self.series, self.period = series, period


@dataclass(frozen=True)
class IndexRatio:
    """Fiyat × (pay / payda). Güncellemede pay güncel, indirgemede pay temel dönemdir."""
    series: str
    numerator_period: str
    numerator: Decimal
    denominator_period: str
    denominator: Decimal

    @property
    def factor(self) -> Decimal:
        return CALC_CONTEXT.divide(self.numerator, self.denominator)

    def describe(self) -> str:
        return (f"{self.series}: {period_label(self.numerator_period)} ({self.numerator}) / "
                f"{period_label(self.denominator_period)} ({self.denominator})")


def ratio(lookup: IndexLookup, series: str, numerator_period: str, denominator_period: str) -> IndexRatio:
    values = []
    for p in (numerator_period, denominator_period):
        v = lookup(series, p)
        if v is None:
            raise MissingIndexError(series, p)
        v = to_decimal(v)
        if v <= 0:
            raise ValueError(f"Endeks değeri sıfırdan büyük olmalıdır: {series} {period_label(p)}")
        values.append(v)
    return IndexRatio(series, numerator_period, values[0], denominator_period, values[1])


def apply_factor(price: Decimal, factor: Decimal, places: int = 2, mode: str = ROUND_HALF_UP) -> Decimal:
    """Katsayı yuvarlanmaz; yalnızca sonuç birim fiyat yuvarlanır."""
    return quantize(CALC_CONTEXT.multiply(to_decimal(price), factor), places, mode)


# --- A. yaklaşık maliyette güncelleme (YİİUY md.10) ----------------------
def ym_periods(price_valid_from: date | str, ym_date: date | str) -> tuple[str, str]:
    """(temel, güncel) dönem: fiyatın yürürlük ayından ve yaklaşık maliyet hesap ayından bir önceki ay."""
    return previous_period(period_of(price_valid_from)), previous_period(period_of(ym_date))


def ym_ratio(lookup: IndexLookup, series: str, price_valid_from: date | str, ym_date: date | str) -> IndexRatio | None:
    """Güncellenmiş birim fiyat = birim fiyat × (güncel / temel). Fiyat hesap tarihi itibarıyla günceldir
    (temel dönem güncel dönemden sonra değil, aynı ya da sonra) ise None döner: güncelleme yapılmaz."""
    base, current = ym_periods(price_valid_from, ym_date)
    if base >= current:
        return None
    return ratio(lookup, series, current, base)


# --- B. sözleşme sürecinde yeni birim fiyat (YİGŞ md.22) ------------------
class ItemKind(StrEnum):
    KURUM_POZU = "KURUM_POZU"
    OZEL_IMALAT = "OZEL_IMALAT"

    @property
    def label(self) -> str:
        return {"KURUM_POZU": "Kamu kurum pozu", "OZEL_IMALAT": "Özel imalat (piyasa fiyatı)"}[self.value]


class OverheadMethod(StrEnum):
    SOZLESME = "SOZLESME"      # sözleşmede belirlenmiş kâr ve genel gider oranı
    BENZER_POZ = "BENZER_POZ"  # benzer poz kırım (tenzilat) oranı
    YOK = "YOK"                # oran ve benzer poz yok: %25 ile bulunan fiyat × 0,90

    @property
    def label(self) -> str:
        return {"SOZLESME": "Sözleşmedeki kâr ve genel gider oranı", "BENZER_POZ": "Benzer poz kırım oranı",
                "YOK": "Oran belirlenmemiş, benzer poz yok (0,90 kuralı)"}[self.value]


@dataclass(frozen=True)
class Component:
    """Analiz bileşeni (rayiç). `series` boşsa fiyat tespit edilen döneme ait güncel fiyattır (güncellenmez)."""
    description: str
    amount: Decimal                 # karsız tutar (miktar × rayiç)
    published: str | None = None    # rayicin yayımlandığı tarih
    series: str | None = None


@dataclass(frozen=True)
class NewPriceInput:
    kind: ItemKind
    price_difference: bool          # işte fiyat farkı veriliyor mu
    tender_date: str
    approval_date: str | None = None        # proje değişikliği onay tarihi
    determination_date: str | None = None   # fiyat tespit tarihi (özel imalat)
    components: tuple[Component, ...] = ()  # kurum pozu
    market_price: Decimal | None = None     # özel imalat: fiyat tespit tarihindeki fiyat
    series: str = GENERAL_SERIES            # özel imalat endeksi
    overhead: OverheadMethod = OverheadMethod.SOZLESME
    contract_rate: Decimal | None = None    # ör. 0.12
    similar_ratio: Decimal | None = None    # benzer poz kırım oranı (teklif / idare fiyatı)
    price_places: int = 2


@dataclass(frozen=True)
class NewPriceResult:
    unit_price: Decimal
    steps: list[str] = field(default_factory=list)
    ratios: list[IndexRatio] = field(default_factory=list)


def similar_ratio(offer_price: Decimal, authority_price: Decimal) -> Decimal:
    if to_decimal(authority_price) <= 0:
        raise ValueError("Benzer pozun idare fiyatı sıfırdan büyük olmalıdır.")
    return CALC_CONTEXT.divide(to_decimal(offer_price), to_decimal(authority_price))


def _require(value, label: str):
    if value in (None, ""):
        raise ValueError(f"{label} girilmelidir.")
    return value


def _fmt(d: Decimal) -> str:
    return f"{d:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _target_period(inp: NewPriceInput) -> tuple[str, str]:
    """Kurum pozu rayiçlerinin güncelleneceği dönem ve gerekçesi."""
    if inp.price_difference:
        return period_of(_require(inp.tender_date, "İhale tarihi")), "ihale tarihi (fiyat farkı verilen iş)"
    return (period_of(_require(inp.approval_date, "Proje değişikliği onay tarihi")),
            "proje değişikliği onay tarihi (fiyat farkı verilmeyen iş)")


def _apply_overhead(inp: NewPriceInput, base: Decimal, steps: list[str]) -> Decimal:
    places = inp.price_places
    if inp.overhead is OverheadMethod.SOZLESME:
        rate = to_decimal(_require(inp.contract_rate, "Sözleşmedeki kâr ve genel gider oranı"))
        if not 0 <= rate <= 1:
            raise ValueError("Kâr ve genel gider oranı %0 ile %100 arasında olmalıdır.")
        price = quantize(base * (1 + rate), places)
        steps.append(f"Karsız tutar × (1 + %{rate * 100:g} sözleşme kâr ve genel gider) = {_fmt(price)}")
        return price
    with25 = base * (1 + DEFAULT_PUBLIC_OVERHEAD)
    steps.append(f"Kamu kurum pozları için %25 kâr ve genel gider ile fiyat: {_fmt(quantize(with25, places))}")
    if inp.overhead is OverheadMethod.BENZER_POZ:
        k = to_decimal(_require(inp.similar_ratio, "Benzer poz kırım oranı"))
        if not 0 < k <= 2:
            raise ValueError("Kırım oranı 0 ile 2 arasında olmalıdır.")
        if k < SIMILAR_RATIO_LIMIT:
            price = quantize(with25 * k, places)
            steps.append(f"Benzer poz kırım oranı {k:.4f} < 0,90: fiyat × {k:.4f} = {_fmt(price)}")
            return price
        steps.append(f"Benzer poz kırım oranı {k:.4f} ≥ 0,90: 0,90 katsayısı uygulanır")
    price = quantize(with25 * SIMILAR_RATIO_LIMIT, places)
    steps.append(f"Fiyat × 0,90 = {_fmt(price)}")
    return price


def new_unit_price(inp: NewPriceInput, lookup: IndexLookup) -> NewPriceResult:
    steps: list[str] = []
    ratios: list[IndexRatio] = []
    if inp.kind is ItemKind.KURUM_POZU:
        if not inp.components:
            raise ValueError("Kurum pozu için en az bir analiz bileşeni (rayiç) girilmelidir.")
        target, why = _target_period(inp)
        steps.append(f"Rayiçler {period_label(target)} dönemine güncellenir: {why}.")
        base = Decimal(0)
        for c in inp.components:
            amount = to_decimal(c.amount)
            if c.series:
                pub = period_of(_require(c.published, f"'{c.description}' yayım tarihi"))
                if pub < target:
                    r = ratio(lookup, c.series, target, pub)
                    ratios.append(r)
                    amount = CALC_CONTEXT.multiply(amount, r.factor)
                    steps.append(f"{c.description}: {_fmt(to_decimal(c.amount))} × {r.describe()} = {_fmt(amount)}")
                else:
                    steps.append(f"{c.description}: yayım dönemi güncel ({period_label(pub)}); güncellenmedi")
            else:
                steps.append(f"{c.description}: {period_label(target)} güncel rayici kullanıldı = {_fmt(amount)}")
            base += amount
        steps.append(f"Karsız analiz tutarı: {_fmt(base)}")
        price = _apply_overhead(inp, base, steps)
        if not inp.price_difference:
            steps.append("Not: onay yılında rayiçler birden fazla kez yayımlandıysa onay tarihinden önce yayımlanan "
                         "son rayiçler esas alınır.")
        return NewPriceResult(price, steps, ratios)

    market = to_decimal(_require(inp.market_price, "Özel imalat birim fiyatı"))
    det = period_of(_require(inp.determination_date, "Fiyat tespit tarihi"))
    if inp.price_difference:
        ref, why = period_of(_require(inp.tender_date, "İhale tarihi")), "ihale tarihi fiyat seviyesine indirgenir"
    else:
        ref, why = (period_of(_require(inp.approval_date, "Proje değişikliği onay tarihi")),
                    "proje değişikliği onay tarihi fiyat seviyesine indirgenir")
    steps.append(f"Fiyat tespit tarihindeki ({period_label(det)}) fiyat {_fmt(market)}; {why}.")
    if det == ref:
        steps.append("Tespit ve esas dönem aynı ay; indirgeme yapılmadı.")
        return NewPriceResult(quantize(market, inp.price_places), steps, ratios)
    r = ratio(lookup, inp.series, ref, det)
    ratios.append(r)
    price = apply_factor(market, r.factor, inp.price_places)
    steps.append(f"{_fmt(market)} × {r.describe()} = {_fmt(price)}")
    return NewPriceResult(price, steps, ratios)
