"""Satın alma teklif karşılaştırması — saf hesap (veritabanı bilmez).

Hesap:
    satır neti         = teklif miktarı × liste birim fiyatı × (1 − iskonto% / 100)      (2 basamak, yukarı yuvarlama)
    net birim fiyat    = liste birim fiyatı × (1 − iskonto% / 100)
    teklif ara toplam  = Σ satır neti
    KDV hariç toplam   = ara toplam + nakliye + montaj + diğer giderler
    KDV                = KDV hariç toplam × KDV% / 100
Normalizasyon (karşılaştırma esası): her talep kalemi İSTENEN miktar × net birim fiyat ile yeniden hesaplanır;
nakliye/montaj/diğer giderler olduğu gibi eklenir. Miktar farkı, eksik kalem, kapsam uyuşmazlığı ve süresi geçmiş
teklif uyarı olarak gösterilir; eksik kalemli teklifin normalize toplamı "eksik" işaretlenir.
En düşük tutar otomatik olarak kazanan ilan edilmez; karar ve gerekçesi kullanıcıya aittir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.rounding import quantize
from yapimmaliyet.domain.text_tr import normalize_unit

ZERO = Decimal(0)
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class RequestItem:
    id: int
    line_no: int
    description: str
    unit: str
    quantity: Decimal
    spec: str = ""
    pos_code: str = ""      # bağlı poz (kütüphane ya da özel poz); boşsa pozsuz kalem
    pos_source: str = ""    # pozun kaynağı: kitap adı / "Özel poz"


@dataclass(frozen=True)
class OfferLine:
    item_id: int
    quantity: Decimal
    unit: str
    list_price: Decimal
    discount_rate: Decimal = ZERO      # yüzde
    scope_ok: bool = True
    brand_model: str = ""
    note: str = ""

    @property
    def net_unit_price(self) -> Decimal:
        return self.list_price * (1 - self.discount_rate / HUNDRED)

    @property
    def net_amount(self) -> Decimal:
        return quantize(self.quantity * self.net_unit_price, 2)

    @property
    def discount_amount(self) -> Decimal:
        return quantize(self.quantity * self.list_price, 2) - self.net_amount


@dataclass(frozen=True)
class Offer:
    id: int
    firm: str
    lines: tuple[OfferLine, ...]
    vat_rate: Decimal = Decimal(20)
    shipping: Decimal = ZERO
    installation: Decimal = ZERO
    other_costs: Decimal = ZERO
    offer_date: str | None = None
    valid_until: str | None = None
    delivery_days: int | None = None
    payment_terms: str = ""
    warranty: str = ""
    technical_ok: bool | None = None    # None: değerlendirilmedi


@dataclass
class OfferEvaluation:
    offer: Offer
    subtotal: Decimal
    discount_total: Decimal
    total_excl_vat: Decimal
    vat: Decimal
    total_incl_vat: Decimal
    normalized_excl_vat: Decimal
    normalized_lines: dict[int, Decimal]           # talep kalemi → normalize tutar
    complete: bool                                 # bütün talep kalemleri teklif edilmiş mi
    warnings: list[str] = field(default_factory=list)
    weighted_score: Decimal | None = None


@dataclass
class Evaluation:
    items: list[RequestItem]
    offers: list[OfferEvaluation]
    notes: list[str]

    def lowest_complete(self) -> OfferEvaluation | None:
        """Bilgi amaçlı: eksiksiz teklifler arasında normalize KDV hariç en düşük tutar (karar DEĞİLDİR)."""
        done = [o for o in self.offers if o.complete]
        return min(done, key=lambda o: o.normalized_excl_vat) if done else None


def evaluate_offer(items: list[RequestItem], offer: Offer, eval_date: str | None = None) -> OfferEvaluation:
    by_item = {ln.item_id: ln for ln in offer.lines}
    warnings: list[str] = []
    norm: dict[int, Decimal] = {}
    for it in items:
        ln = by_item.get(it.id)
        if ln is None:
            warnings.append(f"Kalem {it.line_no} ({it.description}) teklif edilmemiş.")
            continue
        if normalize_unit(ln.unit) != normalize_unit(it.unit):
            warnings.append(f"Kalem {it.line_no}: birim farklı (istenen {it.unit}, teklif {ln.unit}); normalize "
                            "edilmedi, karşılaştırma dışı.")
            continue
        if ln.quantity != it.quantity:
            warnings.append(f"Kalem {it.line_no}: teklif miktarı {ln.quantity}, istenen {it.quantity}; istenen miktar "
                            "× net birim fiyat ile normalize edildi.")
        if not ln.scope_ok:
            warnings.append(f"Kalem {it.line_no}: teknik kapsam istenenle aynı değil ({ln.note or 'açıklama yok'}).")
        norm[it.id] = quantize(it.quantity * ln.net_unit_price, 2)
    extra = [ln for ln in offer.lines if ln.item_id not in {i.id for i in items}]
    if extra:
        warnings.append(f"{len(extra)} teklif satırı talep kalemleriyle eşleşmiyor; toplama katılmadı.")
    subtotal = sum((ln.net_amount for ln in offer.lines if ln.item_id in {i.id for i in items}), ZERO)
    discount = sum((ln.discount_amount for ln in offer.lines), ZERO)
    costs = offer.shipping + offer.installation + offer.other_costs
    excl = subtotal + costs
    vat = quantize(excl * offer.vat_rate / HUNDRED, 2)
    if eval_date and offer.valid_until and offer.valid_until < eval_date:
        warnings.append(f"Teklif geçerlilik süresi dolmuş ({offer.valid_until}).")
    if offer.technical_ok is False:
        warnings.append("Teknik değerlendirmede UYGUN DEĞİL işaretlenmiş.")
    complete = len(norm) == len(items)
    return OfferEvaluation(offer, subtotal, discount, excl, vat, excl + vat,
                           sum(norm.values(), ZERO) + costs, norm, complete, warnings)


def evaluate(items: list[RequestItem], offers: list[Offer], eval_date: str | None = None,
             weights: dict[int, Decimal] | None = None, scores: dict[tuple[int, int], Decimal] | None = None
             ) -> Evaluation:
    """`weights`: ölçüt → ağırlık; `scores`: (ölçüt, teklif) → puan (0–10). Ağırlıklı puan yalnızca bilgi amaçlıdır."""
    out = [evaluate_offer(items, o, eval_date) for o in offers]
    if weights and scores is not None:
        total_w = sum(weights.values(), ZERO)
        for ev in out:
            got = [(w, scores.get((cid, ev.offer.id))) for cid, w in weights.items()]
            if total_w and all(s is not None for _, s in got):
                ev.weighted_score = quantize(sum((w * s for w, s in got), ZERO) / total_w, 2)
    notes = ["Karşılaştırma esası: istenen miktar × net birim fiyat + nakliye + montaj + diğer giderler (KDV hariç). "
             "KDV ayrıca gösterilir.",
             "En düşük tutar otomatik olarak seçilmez. Teknik uygunluk, teslim süresi, ödeme, garanti ve belirlenen "
             "ölçütler birlikte değerlendirilmeli; karar ve gerekçesi onay yetkilisine aittir.",
             "Teklif fiyatları ÇŞB birim fiyat kütüphanesine aktarılmaz; istenirse ayrı özel fiyat kaydı oluşturulur."]
    return Evaluation(items, out, notes)
