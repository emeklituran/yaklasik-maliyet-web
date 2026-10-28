"""Hakediş hesabı (taşeron ve idari hakediş için ortak, saf hesap: veritabanı ve arayüz bilmez).

Hesap sırası, kullanıcının örnek hakedişlerindeki (Ödeme Cetveli / Hakediş Raporu) düzenle aynıdır:
  A  Sözleşme fiyatlarıyla yapılan işler (kümülatif)      = Σ kalem toplam tutarı
  B  Fiyat farkı (kümülatif)                               = önceki fiyat farkları + bu dönem fiyat farkı
  C  Toplam (A + B);  D  bir önceki hakedişin toplamı;  E  bu hakediş tutarı (C − D)
  F  KDV; kesintiler; ödenecek tutar.
Kalem tutarı: birim fiyatlı kalemde round(toplam miktar × birim fiyat, 2); götürü kalemde round(kalem bedeli ×
toplam gerçekleşme oranı, 2). Önceki tutar aynı fiyatla hesaplanır; bu dönem tutarı = toplam − önceki.

Kesinti oranları ve KDV tabanı sözleşme ayarıdır (örneklerde sözleşmeden sözleşmeye değişmektedir); burada
hiçbir mevzuat oranı sabit değildir. KDV tabanı:
  BRUT: KDV = E × oran; ödenecek = E + KDV − kesintiler  (Gasway, Refleks örnekleri)
  NET : net = E − (KDV'ye dayalı olmayan kesintiler); KDV = net × oran; ödenecek = net + KDV − KDV'ye dayalı
        kesintiler  (Indotek örneği: "KDV (%20) kesintiler sonrası")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from yapimmaliyet.domain.rounding import quantize

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")

KINDS = {"TASERON": "Taşeron hakedişi", "IDARI": "İdari hakediş"}
STATUSES = {"TASLAK": "Taslak", "KONTROL": "Kontrolde", "ONAY": "Onaylandı", "ODENDI": "Ödendi",
            "DUZELTILDI": "Düzeltildi (yerine yeni revizyon)"}
DEDUCTION_BASES = {"MANUEL": "Tutar girilir", "BU_HAKEDIS": "Bu hakediş tutarı × oran",
                   "BU_HAKEDIS_EKSI_AVANS": "(Bu hakediş − avans mahsubu) × oran", "KDV": "KDV × oran"}
VAT_BASES = {"BRUT": "KDV bu hakediş tutarı üzerinden", "NET": "KDV kesintiler sonrası tutar üzerinden"}
ITEM_SOURCES = {"SOZLESME": "Sözleşme eki", "DEGISIKLIK": "Onaylı değişiklik", "YENI_BIRIM_FIYAT": "Yeni birim fiyat",
                "ILAVE": "İlave iş"}
PRICE_DIFF_MODES = {"YOK": "Fiyat farkı yok", "TUTAR": "Tutar girilir", "KATSAYI": "Katsayı: E × B × (Pn − 1)"}


@dataclass(frozen=True)
class Terms:
    model: str
    contract_amount: Decimal
    vat_rate: Decimal
    vat_base: str = "BRUT"
    advance_total: Decimal = ZERO


@dataclass(frozen=True)
class Item:
    id: int
    line_no: int
    code: str | None
    description: str
    unit: str | None
    discipline: str | None
    group_name: str | None
    method: str                      # BIRIM_FIYAT | GOTURU
    contract_qty: Decimal | None
    unit_price: Decimal | None
    lump_amount: Decimal | None
    source: str = "SOZLESME"

    @property
    def price(self) -> Decimal | None:
        """Tutar çarpanı: birim fiyatlı kalemde birim fiyat, götürü kalemde kalem bedeli."""
        return self.lump_amount if self.method == "GOTURU" else self.unit_price


@dataclass(frozen=True)
class LineInput:
    item: Item
    prev_qty: Decimal                # önceki hakedişlerdeki toplam (götürüde oran)
    period_qty: Decimal              # bu dönem (götürüde oran artışı)


@dataclass(frozen=True)
class DeductionType:
    id: int
    name: str
    base: str
    rate: Decimal | None = None
    is_advance: bool = False
    is_guarantee: bool = False


@dataclass(frozen=True)
class DeductionEntry:
    type_id: int
    amount: Decimal
    description: str | None = None


@dataclass(frozen=True)
class PriceDiff:
    mode: str = "YOK"
    amount: Decimal | None = None    # TUTAR
    pn: Decimal | None = None        # KATSAYI: fiyat farkı endeksi Pn
    b: Decimal | None = None         # KATSAYI: sözleşmedeki B katsayısı (ör. 0,90)

    def coefficient(self) -> Decimal | None:
        if self.mode != "KATSAYI" or self.pn is None or self.b is None:
            return None
        return quantize(self.b * (self.pn - ONE), 6)


@dataclass(frozen=True)
class Previous:
    """Bir önceki onaylı hakedişin kümülatif değerleri (ilk hakedişte sıfır)."""
    work_cum: Decimal = ZERO
    price_diff_cum: Decimal = ZERO
    advance_cum: Decimal = ZERO


@dataclass(frozen=True)
class Warning:
    level: str                       # HATA | UYARI
    code: str
    message: str
    item_id: int | None = None


@dataclass(frozen=True)
class LineResult:
    input: LineInput
    cum_qty: Decimal
    cum_amount: Decimal | None
    prev_amount: Decimal | None
    period_amount: Decimal | None

    @property
    def item(self) -> Item:
        return self.input.item


@dataclass(frozen=True)
class DeductionResult:
    type: DeductionType
    base_amount: Decimal | None
    amount: Decimal
    entries: tuple[DeductionEntry, ...] = ()


@dataclass
class PaymentResult:
    lines: list[LineResult]
    work_cum: Decimal
    work_prev: Decimal
    work_period: Decimal
    line_period_sum: Decimal
    price_diff_coef: Decimal | None
    price_diff_period: Decimal
    price_diff_prev: Decimal
    total_cum: Decimal
    total_prev: Decimal
    this_total: Decimal
    vat: Decimal
    net_before_vat: Decimal | None
    deductions: list[DeductionResult]
    deductions_total: Decimal
    payable: Decimal
    advance_period: Decimal
    advance_cum: Decimal
    remaining_contract: Decimal
    warnings: list[Warning] = field(default_factory=list)

    @property
    def price_diff_cum(self) -> Decimal:
        return self.price_diff_prev + self.price_diff_period

    @property
    def price_change_effect(self) -> Decimal:
        """Önceki hakedişin kayıtlı tutarı ile kalemlerin bugünkü fiyatla önceki tutarı arasındaki fark
        (onaylı fiyat değişikliği). Sıfır değilse bu dönem tutarına yansır ve raporda ayrıca gösterilir."""
        return self.work_period - self.line_period_sum

    @property
    def progress(self) -> Decimal | None:
        """Kümülatif iş tutarının sözleşme bedeline oranı (gerçekleşme yüzdesi)."""
        contract = self.remaining_contract + self.work_cum
        return None if contract <= 0 else self.work_cum / contract

    @property
    def errors(self) -> list[Warning]:
        return [w for w in self.warnings if w.level == "HATA"]


def _r(value: Decimal) -> Decimal:
    return quantize(value, 2)


def _line(li: LineInput, warnings: list[Warning]) -> LineResult:
    it = li.item
    cum = li.prev_qty + li.period_qty
    label = f"{it.line_no}. kalem ({it.code or it.description[:30]})"
    if li.period_qty < 0:
        warnings.append(Warning("UYARI", "NEGATIF", f"{label}: bu dönem miktarı negatif ({li.period_qty}); "
                                "düzeltme amaçlı ise açıklama ekleyin.", it.id))
    if cum < 0:
        warnings.append(Warning("HATA", "KUMULATIF_NEGATIF", f"{label}: toplam miktar negatif olamaz ({cum}).", it.id))
    if it.method == "GOTURU":
        if cum > ONE:
            warnings.append(Warning("HATA", "ORAN_ASIMI", f"{label}: götürü kalemin toplam gerçekleşme oranı %100'ü "
                                    f"aşamaz (%{cum * 100:.2f}).", it.id))
    elif it.contract_qty is not None and cum > it.contract_qty:
        extra = "" if not it.contract_qty else f" (%{(cum / it.contract_qty - 1) * 100:.2f} fazla)"
        warnings.append(Warning("UYARI", "MIKTAR_ASIMI", f"{label}: toplam miktar sözleşme miktarını aşıyor: "
                                f"{cum} > {it.contract_qty}{extra}. Onaylı değişiklik / iş artışı gerekir.", it.id))
    price = it.price
    if price is None:
        if cum or li.period_qty:
            warnings.append(Warning("HATA", "FIYAT_EKSIK", f"{label}: birim fiyat girilmemiş; tutar hesaplanamaz.",
                                    it.id))
        return LineResult(li, cum, None, None, None)
    cum_amount, prev_amount = _r(cum * price), _r(li.prev_qty * price)
    return LineResult(li, cum, cum_amount, prev_amount, cum_amount - prev_amount)


def _deductions(types: list[DeductionType], entries: list[DeductionEntry], this_total: Decimal,
                vat_for: callable, vat_base: str) -> tuple[list[DeductionResult], Decimal, Decimal | None, Decimal]:
    by_type: dict[int, list[DeductionEntry]] = {}
    for e in entries:
        by_type.setdefault(e.type_id, []).append(e)
    manual = {t.id: sum((e.amount for e in by_type.get(t.id, [])), ZERO) for t in types if t.base == "MANUEL"}
    advance = sum((manual[t.id] for t in types if t.is_advance), ZERO)
    results: dict[int, DeductionResult] = {}
    for t in types:
        if t.base == "MANUEL":
            results[t.id] = DeductionResult(t, None, _r(manual[t.id]), tuple(by_type.get(t.id, [])))
        elif t.base in ("BU_HAKEDIS", "BU_HAKEDIS_EKSI_AVANS"):
            base = this_total - (advance if t.base == "BU_HAKEDIS_EKSI_AVANS" else ZERO)
            results[t.id] = DeductionResult(t, base, _r(base * t.rate))
    non_vat = sum((r.amount for r in results.values()), ZERO)
    net = this_total - non_vat if vat_base == "NET" else None
    vat = vat_for(net if net is not None else this_total)
    for t in types:
        if t.base == "KDV":
            results[t.id] = DeductionResult(t, vat, _r(vat * t.rate))
    ordered = [results[t.id] for t in types]
    return ordered, vat, net, advance


def compute(terms: Terms, lines: list[LineInput], types: list[DeductionType], entries: list[DeductionEntry],
            price_diff: PriceDiff = PriceDiff(), prev: Previous = Previous()) -> PaymentResult:
    warnings: list[Warning] = []
    results = [_line(li, warnings) for li in lines]
    work_cum = sum((r.cum_amount or ZERO for r in results), ZERO)
    line_period_sum = sum((r.period_amount or ZERO for r in results), ZERO)
    work_period = work_cum - prev.work_cum
    coef = price_diff.coefficient()
    if price_diff.mode == "TUTAR":
        if price_diff.amount is None:
            warnings.append(Warning("HATA", "FF_EKSIK", "Fiyat farkı 'tutar' olarak seçilmiş ama tutar girilmemiş."))
        ff = _r(price_diff.amount or ZERO)
    elif price_diff.mode == "KATSAYI":
        if coef is None:
            warnings.append(Warning("HATA", "FF_EKSIK", "Fiyat farkı katsayısı için Pn ve B girilmelidir."))
        ff = _r(work_period * (coef or ZERO))
    else:
        ff = ZERO
    this_total = work_period + ff
    total_cum, total_prev = work_cum + prev.price_diff_cum + ff, prev.work_cum + prev.price_diff_cum
    deds, vat, net, advance = _deductions(types, entries, this_total, lambda base: _r(base * terms.vat_rate),
                                          terms.vat_base)
    ded_total = sum((d.amount for d in deds), ZERO)
    payable = this_total + vat - ded_total
    result = PaymentResult(results, work_cum, prev.work_cum, work_period, line_period_sum, coef, ff,
                           prev.price_diff_cum, total_cum, total_prev, this_total, vat, net, deds, ded_total, payable,
                           advance, prev.advance_cum + advance, terms.contract_amount - work_cum, warnings)
    _totals_checks(terms, lines, result, prev)
    return result


def _totals_checks(terms: Terms, lines: list[LineInput], r: PaymentResult, prev: Previous) -> None:
    w = r.warnings
    if terms.contract_amount > 0 and r.work_cum > terms.contract_amount:
        w.append(Warning("UYARI", "BEDEL_ASIMI", f"Kümülatif iş tutarı sözleşme bedelini aşıyor "
                         f"(%{(r.work_cum / terms.contract_amount - 1) * 100:.2f}). İş artışı onayı gerekir."))
    if abs(r.price_change_effect) >= CENT:
        w.append(Warning("UYARI", "FIYAT_DEGISIMI", "Önceki hakedişin kayıtlı tutarı ile kalemlerin bugünkü fiyatla "
                         f"önceki tutarı farklı ({r.price_change_effect}); fiyat değişikliği bu döneme yansıdı."))
    if r.advance_cum > terms.advance_total:
        w.append(Warning("UYARI", "AVANS_ASIMI", f"Avans mahsubu toplamı ({r.advance_cum}) verilen avansı "
                         f"({terms.advance_total}) aşıyor."))
    if r.payable < 0:
        w.append(Warning("UYARI", "NEGATIF_ODEME", "Kesintiler hakediş tutarını aşıyor; ödenecek tutar negatif."))
    lump = sum((li.item.lump_amount or ZERO for li in lines if li.item.method == "GOTURU"), ZERO)
    unit = sum((_r((li.item.contract_qty or ZERO) * (li.item.unit_price or ZERO))
                for li in lines if li.item.method == "BIRIM_FIYAT" and li.item.source == "SOZLESME"), ZERO)
    if terms.model == "ANAHTAR_TESLIM" and lines and abs(lump - terms.contract_amount) >= CENT:
        w.append(Warning("UYARI", "AGIRLIK", f"Götürü kalem bedelleri toplamı ({lump}) sözleşme bedeline "
                         f"({terms.contract_amount}) eşit değil; ağırlıklar (pursantaj) tutarsız."))
    if terms.model == "KARMA" and lines and abs(lump + unit - terms.contract_amount) >= CENT:
        w.append(Warning("UYARI", "AGIRLIK", f"Götürü bedeller ({lump}) + sözleşme miktarı × birim fiyat ({unit}) "
                         f"sözleşme bedeline ({terms.contract_amount}) eşit değil."))
    methods = {li.item.method for li in lines}
    if terms.model == "BIRIM_FIYAT" and "GOTURU" in methods:
        w.append(Warning("UYARI", "MODEL", "Birim fiyatlı sözleşmede götürü kalem var; sözleşme modeli karma olmalı."))
    if terms.model == "ANAHTAR_TESLIM" and "BIRIM_FIYAT" in methods:
        w.append(Warning("UYARI", "MODEL", "Anahtar teslim sözleşmede birim fiyatlı kalem var; model karma olmalı."))
    seen: dict[str, int] = {}
    for li in lines:
        key = (li.item.code or "").strip().upper()
        if key and key in seen:
            w.append(Warning("UYARI", "MUKERRER_KALEM", f"Aynı poz numarası iki kalemde: {li.item.code} "
                             f"({seen[key]}. ve {li.item.line_no}. kalem).", li.item.id))
        seen.setdefault(key, li.item.line_no)


def group_totals(result: PaymentResult, key) -> list[tuple[str, Decimal, Decimal, Decimal, Decimal]]:
    """Kalemleri `key(item)` ile gruplar: (grup, sözleşme tutarı, önceki, bu dönem, toplam)."""
    order: list[str] = []
    acc: dict[str, list[Decimal]] = {}
    for ln in result.lines:
        k = key(ln.item) or "Belirtilmemiş"
        if k not in acc:
            order.append(k)
            acc[k] = [ZERO, ZERO, ZERO, ZERO]
        it = ln.item
        contract = it.lump_amount if it.method == "GOTURU" else _r((it.contract_qty or ZERO) * (it.unit_price or ZERO))
        for i, v in enumerate((contract or ZERO, ln.prev_amount or ZERO, ln.period_amount or ZERO,
                               ln.cum_amount or ZERO)):
            acc[k][i] += v
    return [(k, *acc[k]) for k in order]


def pn_from_indices(weights: dict[str, Decimal], base: dict[str, Decimal], current: dict[str, Decimal]) -> Decimal:
    """Pn = Σ ağırlık × (güncel endeks / temel endeks), 6 basamak (Indotek örneğindeki 6-F.F. KATSAYI sayfası).
    Ağırlıklar ve endeksler sözleşmeden / TÜİK'ten kullanıcı tarafından girilir; burada varsayılan değer yoktur."""
    total_w = sum(weights.values(), ZERO)
    if abs(total_w - ONE) > Decimal("0.000001"):
        raise ValueError(f"Fiyat farkı ağırlıklarının toplamı 1 olmalıdır (şu an {total_w}).")
    acc = ZERO
    for k, wt in weights.items():
        if not wt:
            continue
        if not base.get(k) or current.get(k) is None:
            raise ValueError(f"'{k}' için temel ve güncel endeks girilmelidir.")
        acc += wt * current[k] / base[k]
    return quantize(acc, 6)
