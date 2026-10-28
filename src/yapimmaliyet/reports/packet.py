"""Çok sayfalı belge dosyası (hakediş dosyası) modeli: her sayfa ayrı bir belgedir (kapak, ödeme cetveli, icmal…).

Excel'de her sayfa ayrı çalışma sayfası, PDF'de yeni sayfa olur. Biçimden bağımsızdır; yazıcılar packet_xlsx.py ve
packet_pdf.py. Tablo sütunları reports.model.Col/Table ile aynıdır (biçim türü ve göreli genişlik).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yapimmaliyet.reports.model import Table


@dataclass(frozen=True)
class KeyValues:
    """İki sütunlu bilgi bloğu (kapaktaki 'Sözleşme bedeli : …' satırları)."""
    rows: list[tuple[str, str]]


@dataclass(frozen=True)
class Text:
    text: str
    style: str = "normal"        # normal | heading | note | warning


@dataclass(frozen=True)
class TableBlock:
    table: Table
    title: str | None = None
    bold_rows: tuple[int, ...] = ()     # ara toplam / grup başlığı satırları (0 tabanlı)


@dataclass(frozen=True)
class Signature:
    role: str                    # ör. "Yüklenici", "Kontrol Mühendisi"
    name: str = ""
    title: str = ""


@dataclass(frozen=True)
class Signatures:
    items: list[Signature]
    date_text: str = ""


Block = KeyValues | Text | TableBlock | Signatures


@dataclass
class Page:
    sheet: str                   # Excel sayfa adı (en çok 31 karakter)
    title: str
    blocks: list[Block]
    header: list[tuple[str, str]] = field(default_factory=list)   # sağ üst: Hakediş No, Tarih…
    landscape: bool = False


@dataclass
class Packet:
    title: str
    subtitle: str
    pages: list[Page]
    draft: bool
    footer_note: str

    def sheet_names(self) -> list[str]:
        return [p.sheet for p in self.pages]
