"""Kütüphane veri sınıfları (arayüz ve veritabanından bağımsız)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

PRICE_DB_PLACES = 4  # kaynak fiyatlar en fazla 4 ondalık basamakla saklanır
QUANTITY_PLACES = 3  # miktarlar en fazla 3 ondalık basamakla saklanır


class BookStatus(StrEnum):
    TASLAK = "TASLAK"
    AKTIF = "AKTIF"
    ARSIV = "ARSIV"

    @property
    def label(self) -> str:
        return {"TASLAK": "Taslak", "AKTIF": "Aktif", "ARSIV": "Arşiv"}[self.value]


# İzin verilen durum geçişleri. Taslağa dönüş ayrıca "kullanımda değil" koşuluna bağlıdır.
BOOK_TRANSITIONS: dict[BookStatus, set[BookStatus]] = {
    BookStatus.TASLAK: {BookStatus.AKTIF},
    BookStatus.AKTIF: {BookStatus.ARSIV, BookStatus.TASLAK},
    BookStatus.ARSIV: {BookStatus.AKTIF, BookStatus.TASLAK},
}


@dataclass(frozen=True)
class Authority:
    id: int
    name: str
    short_name: str | None
    notes: str | None


@dataclass(frozen=True)
class PriceBook:
    id: int
    authority_id: int
    authority_name: str
    title: str
    year: int
    edition: str
    valid_from: str | None
    status: BookStatus
    notes: str | None
    price_count: int
    discipline: str | None = None   # INSAAT | ELEKTRIK | MEKANIK | ALTYAPI (boş: belirtilmemiş)
    period: str | None = None           # fiyat dönemi ("2026 Ocak", "2026/2"); kaynakta yoksa None (Belirtilmemiş)
    valid_to: str | None = None
    published_on: str | None = None     # yayın tarihi (geçerlilik ve içe aktarma tarihinden ayrı)
    revision_no: str | None = None
    revised_on: str | None = None
    price_kind: str = "BELIRTILMEMIS"
    verification: str = "KULLANICI"
    source_ref: str | None = None
    authority_code: str | None = None

    @property
    def display_name(self) -> str:
        ed = f" ({self.edition})" if self.edition else ""
        return f"{self.title} {self.year}{ed}"


@dataclass(frozen=True)
class NewPositionPrice:
    code: str
    description: str
    unit: str
    unit_price: Decimal
    category_path: str | None = None
    source_page: int | None = None
    source_row: int | None = None
    raw_text: str | None = None
    ocr_confidence: float | None = None
    montage_price: Decimal | None = None   # ÇŞB montajlı listelerde "Montaj Bedeli"; kaynakta yoksa None
    printed_page: str | None = None        # belgede basılı sayfa numarası (source_page: PDF sayfası)


@dataclass(frozen=True)
class PositionPrice:
    id: int
    position_id: int
    authority_id: int
    authority_name: str
    book_id: int
    book_title: str
    book_year: int
    book_edition: str
    book_status: BookStatus
    code: str
    description: str
    unit: str
    unit_price: Decimal
    category_path: str | None
    source_page: int | None
    source_row: int | None
    montage_price: Decimal | None = None
    period: str | None = None
    valid_from: str | None = None
    price_kind: str = "BELIRTILMEMIS"
    verification: str = "KULLANICI"
    source_ref: str | None = None
    printed_page: str | None = None
    authority_code: str | None = None


@dataclass(frozen=True)
class PriceHistoryEntry:
    price_id: int
    book_id: int
    book_title: str
    year: int
    edition: str
    status: BookStatus
    description: str
    unit: str
    unit_price: Decimal
    authority_name: str = ""
    period: str | None = None
    valid_from: str | None = None
    price_kind: str = "BELIRTILMEMIS"


SORT_KEYS = ("authority", "book", "code", "description", "unit", "price")


@dataclass(frozen=True)
class SearchFilter:
    text: str = ""
    authority_id: int | None = None
    book_id: int | None = None
    year: int | None = None
    category: str | None = None
    include_archived: bool = True
    sort: str = "code"
    descending: bool = False
    book_ids: tuple[int, ...] | None = None   # birden çok kitapla sınırla (None: kısıt yok)
    exclude_drafts: bool = False
    period: str | None = None
    price_kind: str | None = None
    verification: str | None = None


class PriceMethod(StrEnum):
    DOGRUDAN = "DOGRUDAN"
    ANALIZ = "ANALIZ"

    @property
    def label(self) -> str:
        return {"DOGRUDAN": "Doğrudan girilen fiyat", "ANALIZ": "Analizden hesaplanan fiyat"}[self.value]


@dataclass(frozen=True)
class CustomVersion:
    id: int
    custom_position_id: int
    version_no: int
    description: str
    unit: str
    price_method: PriceMethod
    direct_price: Decimal | None
    computed_price: Decimal | None
    price_date: str
    basis_text: str
    locked: bool
    created_at: str

    @property
    def effective_price(self) -> Decimal | None:
        return self.direct_price if self.price_method == PriceMethod.DOGRUDAN else self.computed_price


@dataclass(frozen=True)
class CustomPosition:
    id: int
    code: str
    name: str
    category: str | None
    status: str
    project_id: int | None
    created_at: str
    current: CustomVersion | None
