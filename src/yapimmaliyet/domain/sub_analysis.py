"""Alt analiz (iç içe özel poz) çözümleme ve döngü denetimi.

Bir analiz girdisi başka bir özel pozu (`ref_custom_position_id`) kaynak alabilir. Bu modül saf
fonksiyonlardır (veritabanı yok): pozun verisi `Loader` ile dışarıdan sağlanır.

Fiyat politikası: alt analiz fiyatı hesap anında, alt pozun GÜNCEL sürümünden çözülür
(doğrudan fiyatlıysa o fiyat; analizliyse analizi, kendi alt analizleri de güncel çözülerek yeniden
hesaplanır). Sürümlere kaydedilmiş fiyatlar (computed_price, girdi birim fiyatı) kaydedildiği andaki
değerdir ve kilitli sürümler sonradan değişmez.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from yapimmaliyet.domain.analysis import AnalysisSpec, compute

MAX_DEPTH = 8  # en fazla kaç alt analiz seviyesi (kök hariç)


class SubAnalysisError(ValueError):
    """Alt analiz çözümlenemedi."""


class CycleError(SubAnalysisError):
    def __init__(self, path: tuple[str, ...]):
        self.path = path
        super().__init__("Döngü: " + " → ".join(path))


class DepthError(SubAnalysisError):
    def __init__(self, path: tuple[str, ...]):
        self.path = path
        super().__init__(f"Alt analiz iç içe en fazla {MAX_DEPTH} seviye olabilir: " + " → ".join(path))


@dataclass(frozen=True)
class PositionNode:
    """Bir özel pozun güncel sürümü: analizliyse `spec`, değilse `unit_price` kullanılır."""
    code: str
    unit_price: Decimal | None
    spec: AnalysisSpec | None = None


Loader = Callable[[int], PositionNode]
Path = tuple[tuple[int, str], ...]  # (poz id, kod) zinciri


def _labels(path: Path, tail: str) -> tuple[str, ...]:
    return tuple(code for _, code in path) + (tail,)


def resolve_spec(spec: AnalysisSpec, load: Loader, path: Path = ()) -> AnalysisSpec:
    """Alt analiz girdilerinin birim fiyatlarını güncel değerlerle doldurur.

    `path`, çağıran pozun zinciridir (kök için kendisi). Zincirde olan bir pozu yeniden kaynak almak
    döngüdür (kendine gönderme dahil)."""
    items = []
    for it in spec.items:
        ref = it.ref_custom_position_id
        if ref is None:
            items.append(it)
            continue
        node = load(ref)
        ids = [pid for pid, _ in path]
        if ref in ids:
            start = ids.index(ref)
            raise CycleError(_labels(path[start:], node.code))
        if len(path) > MAX_DEPTH:
            raise DepthError(_labels(path, node.code))
        items.append(replace(it, unit_price=resolve_node_price(ref, node, load, path)))
    return replace(spec, items=tuple(items))


def resolve_node_price(pos_id: int, node: PositionNode, load: Loader, path: Path = ()) -> Decimal:
    if node.spec is None:
        if node.unit_price is None:
            raise SubAnalysisError(f"{node.code} pozunun fiyatı yok.")
        return node.unit_price
    resolved = resolve_spec(node.spec, load, path + ((pos_id, node.code),))
    try:
        return compute(resolved).unit_price
    except ValueError as exc:
        raise SubAnalysisError(f"{node.code} analizi hesaplanamadı: {exc}") from exc


def resolve_unit_price(pos_id: int, load: Loader) -> Decimal:
    """Bir özel pozun güncel çözümlenmiş birim fiyatı (iç içe alt analizler dahil)."""
    return resolve_node_price(pos_id, load(pos_id), load)
