"""Birim fiyat kütüphanesi iş akışları: idareler, kitaplar, poz fiyatları, arama.

Kurallar:
- Fiyat kayıtları yalnızca TASLAK durumundaki kitapta eklenir/değiştirilir/silinir.
  Aktif ya da arşivdeki kitaplar salt okunurdur; böylece geçmiş fiyatlar sessizce değişmez.
- Kullanımda olan (keşif kalemi ya da analiz girdisi tarafından başvurulan) bir kitap
  taslağa alınamaz ve silinemez.
- Yeni kitap ya da yeni sürüm eski kayıtları değiştirmez; her kitap kendi kayıtlarını taşır.
- Her yazma işlemi tek transaction içinde yapılır ve değişiklik kaydına yazılır.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, authorities, positions, price_books, rate_positions
from yapimmaliyet.domain.models import (
    BOOK_TRANSITIONS,
    Authority,
    BookStatus,
    NewPositionPrice,
    PositionPrice,
    PriceBook,
    PriceHistoryEntry,
    SearchFilter,
)
from yapimmaliyet.domain.periods import PRICE_KINDS, UNVERIFIED, VERIFICATION, verification_label
from yapimmaliyet.domain.tender import DISCIPLINES
from yapimmaliyet.domain.text_tr import normalize_code, normalize_search
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean as _clean
from yapimmaliyet.services.validators import require as _require
from yapimmaliyet.services.validators import validate_iso_date, validate_price


def _validate_new_price(item: NewPositionPrice) -> NewPositionPrice:
    code = _require(item.code, "Poz kodu")
    if not normalize_code(code):
        raise ValidationError("Poz kodu geçersiz.")
    return NewPositionPrice(code, _require(item.description, "Poz tanımı"), _require(item.unit, "Birim"),
                            validate_price(item.unit_price), _clean(item.category_path),
                            item.source_page, item.source_row, item.raw_text, item.ocr_confidence,
                            None if item.montage_price is None else validate_price(item.montage_price),
                            _clean(item.printed_page))


class LibraryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- idareler ----------------------------------------------------------
    def list_authorities(self) -> list[Authority]:
        return authorities.list_all(self.conn)

    def _check_authority_name(self, name: str, exclude_id: int | None = None) -> None:
        key = normalize_search(name)
        for a in authorities.list_all(self.conn):
            if a.id != exclude_id and normalize_search(a.name) == key:
                raise ConflictError(f"Aynı ya da yazımı çok benzer bir idare zaten kayıtlı: {a.name}")

    def add_authority(self, name: str, short_name: str | None = None, notes: str | None = None) -> int:
        name = _require(name, "İdare adı")
        self._check_authority_name(name)
        with transaction(self.conn):
            new_id = authorities.insert(self.conn, name, _clean(short_name), _clean(notes))
            audit.log(self.conn, "authority", new_id, "EKLE", new={"name": name})
        return new_id

    def update_authority(self, authority_id: int, name: str, short_name: str | None, notes: str | None) -> None:
        old = self._authority(authority_id)
        name = _require(name, "İdare adı")
        self._check_authority_name(name, exclude_id=authority_id)
        with transaction(self.conn):
            authorities.update(self.conn, authority_id, name, _clean(short_name), _clean(notes))
            audit.log(self.conn, "authority", authority_id, "GUNCELLE",
                      old={"name": old.name, "short_name": old.short_name}, new={"name": name})

    def delete_authority(self, authority_id: int) -> None:
        old = self._authority(authority_id)
        if authorities.usage_count(self.conn, authority_id):
            raise ConflictError(f"'{old.name}' idaresine bağlı fiyat kitabı ya da proje var; silinemez.")
        with transaction(self.conn):
            authorities.delete(self.conn, authority_id)
            audit.log(self.conn, "authority", authority_id, "SIL", old={"name": old.name})

    def _authority(self, authority_id: int) -> Authority:
        a = authorities.get(self.conn, authority_id)
        if a is None:
            raise NotFoundError("İdare bulunamadı.")
        return a

    # --- fiyat kitapları ---------------------------------------------------
    def list_books(self, authority_id: int | None = None) -> list[PriceBook]:
        return price_books.list_books(self.conn, authority_id)

    def get_book(self, book_id: int) -> PriceBook:
        b = price_books.get(self.conn, book_id)
        if b is None:
            raise NotFoundError("Fiyat kitabı bulunamadı.")
        return b

    def _validate_book(self, authority_id: int, title: str, year: int, edition: str | None,
                       valid_from: str | None, exclude_id: int | None = None) -> tuple[str, int, str, str | None]:
        self._authority(authority_id)
        title = _require(title, "Kitap adı")
        if not isinstance(year, int) or not 1950 <= year <= 2200:
            raise ValidationError("Kitap yılı 1950 ile 2200 arasında olmalıdır.")
        edition = (edition or "").strip()
        if price_books.exists(self.conn, authority_id, year, edition, exclude_id):
            ed = f" / {edition}" if edition else ""
            raise ConflictError(f"Bu idarenin {year}{ed} kitabı zaten kayıtlı. Farklı bir sürüm adı girin.")
        return title, year, edition, validate_iso_date(valid_from, "Yürürlük tarihi")

    def validate_new_book(self, authority_id: int, title: str, year: int, edition: str | None,
                          valid_from: str | None) -> tuple[str, int, str, str | None]:
        """Yeni kitap bilgilerini denetler ve temizlenmiş hâlini döndürür (kayıt yapmaz)."""
        return self._validate_book(authority_id, title, year, edition, valid_from)

    def add_book(self, authority_id: int, title: str, year: int, edition: str | None = None,
                 valid_from: str | None = None, notes: str | None = None) -> int:
        title, year, edition, valid_from = self._validate_book(authority_id, title, year, edition, valid_from)
        with transaction(self.conn):
            new_id = price_books.insert(self.conn, authority_id, title, year, edition, valid_from, _clean(notes))
            audit.log(self.conn, "price_book", new_id, "EKLE",
                      new={"title": title, "year": year, "edition": edition})
        return new_id

    def update_book(self, book_id: int, title: str, year: int, edition: str | None,
                    valid_from: str | None, notes: str | None) -> None:
        old = self.get_book(book_id)
        if old.status != BookStatus.TASLAK and (year != old.year or (edition or "") != old.edition):
            raise ConflictError("Yıl ve sürüm yalnızca taslak kitaplarda değiştirilebilir.")
        title, year, edition, valid_from = self._validate_book(
            old.authority_id, title, year, edition, valid_from, exclude_id=book_id)
        with transaction(self.conn):
            price_books.update(self.conn, book_id, title, year, edition, valid_from, _clean(notes))
            audit.log(self.conn, "price_book", book_id, "GUNCELLE",
                      old={"title": old.title, "year": old.year, "edition": old.edition},
                      new={"title": title, "year": year, "edition": edition})

    def set_book_discipline(self, book_id: int, discipline: str | None) -> None:
        """Kitabın disiplini (inşaat/elektrik/mekanik/altyapı); fiyatları değiştirmez, her durumda ayarlanabilir."""
        if discipline is not None and discipline not in DISCIPLINES:
            raise ValidationError("Geçersiz disiplin.")
        old = self.get_book(book_id)
        with transaction(self.conn):
            price_books.set_discipline(self.conn, book_id, discipline)
            audit.log(self.conn, "price_book", book_id, "GUNCELLE", old={"discipline": old.discipline},
                      new={"discipline": discipline})

    def set_book_status(self, book_id: int, status: BookStatus) -> None:
        book = self.get_book(book_id)
        if status == book.status:
            return
        if status not in BOOK_TRANSITIONS[book.status]:
            raise ConflictError(f"'{book.status.label}' durumundaki kitap '{status.label}' durumuna alınamaz.")
        if status == BookStatus.AKTIF and book.price_count == 0:
            raise ConflictError("Poz kaydı olmayan kitap aktifleştirilemez.")
        if status == BookStatus.AKTIF and book.verification in UNVERIFIED:
            raise ConflictError(f"Kitap '{verification_label(book.verification)}' durumunda; doğrulanmamış fiyatlar "
                                "hesaplamada kullanılamaz. Önce kaynakla karşılaştırıp doğrulama durumunu değiştirin.")
        # keşifler pozları kendi kopyasıyla taşıdığından kullanımdaki kitap da taslağa alınıp düzeltilebilir;
        # keşiflere yansıtmak kullanıcının 'Birim Fiyatları Güncelle' işlemiyle yapılır
        with transaction(self.conn):
            price_books.set_status(self.conn, book_id, status)
            audit.log(self.conn, "price_book", book_id, "GUNCELLE",
                      old={"status": book.status.value}, new={"status": status.value})

    def update_book_meta(self, book_id: int, **meta) -> None:
        """Fiyat dönemi, geçerlilik bitişi, yayın/revizyon tarihi ve no, fiyat türü, doğrulama durumu, kaynak belge.

        Tarihler ISO (yyyy-aa-gg) ya da boş; boş değer 'Belirtilmemiş' demektir. Aktif kitap doğrulanmamış yapılamaz."""
        book = self.get_book(book_id)
        clean: dict = {}
        for key, value in meta.items():
            if key not in price_books.META_FIELDS:
                raise ValidationError(f"Bilinmeyen kitap alanı: {key}")
            value = _clean(value) if isinstance(value, str) or value is None else value
            if key in ("valid_to", "published_on", "revised_on"):
                value = validate_iso_date(value, {"valid_to": "Geçerlilik bitişi", "published_on": "Yayın tarihi",
                                                  "revised_on": "Revizyon tarihi"}[key])
            if key == "price_kind" and (value or "BELIRTILMEMIS") not in PRICE_KINDS:
                raise ValidationError("Fiyat türü geçersiz.")
            if key == "verification" and value not in VERIFICATION:
                raise ValidationError("Doğrulama durumu geçersiz.")
            clean[key] = value if key != "price_kind" else (value or "BELIRTILMEMIS")
        if clean.get("verification") in UNVERIFIED and book.status == BookStatus.AKTIF:
            raise ConflictError("Aktif kitap doğrulanmamış olarak işaretlenemez; önce taslağa alın.")
        valid_to, start = clean.get("valid_to", book.valid_to), book.valid_from
        if valid_to and start and valid_to < start:
            raise ValidationError("Geçerlilik bitişi başlangıçtan önce olamaz.")
        with transaction(self.conn):
            price_books.set_meta(self.conn, book_id, **clean)
            audit.log(self.conn, "price_book", book_id, "GUNCELLE", new=clean)

    def periods(self) -> list[str]:
        return positions.periods(self.conn)

    def analyses_for_code(self, code: str, authority_id: int | None = None) -> list[sqlite3.Row]:
        """Pozun resmî analizleri (aynı kurum öncelikli): kitap, dönem, birim fiyat, aritmetik kontrol, bileşen sayısı."""
        return self.conn.execute(
            "SELECT oa.id, oa.code, oa.name, oa.unit, oa.unit_price, oa.montage_price, oa.material_total,"
            " oa.labour_total, oa.page, oa.check_ok, ab.title, ab.year, ab.edition, ab.period, ab.valid_from,"
            " COALESCE(a.short_name, a.name) AS authority,"
            " (SELECT COUNT(*) FROM official_analysis_items i WHERE i.analysis_id = oa.id) AS item_count"
            " FROM official_analyses oa JOIN analysis_books ab ON ab.id = oa.analysis_book_id"
            " JOIN authorities a ON a.id = ab.authority_id WHERE oa.code_norm = ?"
            " ORDER BY ab.authority_id IS NOT ?, ab.year DESC", (normalize_code(code), authority_id)).fetchall()

    def analysis_items(self, analysis_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT line_no, section, code, description, unit, quantity, quantity_text, unit_price, amount, amount_column"
            " FROM official_analysis_items WHERE analysis_id = ? ORDER BY line_no", (analysis_id,)).fetchall()

    def book_log(self, book_id: int) -> list[sqlite3.Row]:
        """Kitabın güncelleme geçmişi (değişiklik kaydı): oluşturma, durum ve dönem değişiklikleri."""
        return audit.recent(self.conn, "price_book", book_id, 50)

    def delete_book(self, book_id: int) -> None:
        """Kitabı fiyatlarıyla siler. Durumu ne olursa olsun, hiçbir keşif ya da analiz kullanmıyorsa silinebilir;
        kullanımdaysa kayıtlar bozulmasın diye silinmez (kitap Arşiv durumuna alınarak eşleştirmeden çıkarılır)."""
        book = self.get_book(book_id)
        with transaction(self.conn):
            kept = price_books.keep_referenced_prices(self.conn, book_id)
            price_books.delete_with_prices(self.conn, book_id)
            positions.delete_orphan_positions(self.conn)
            audit.log(self.conn, "price_book", book_id, "SIL",
                      old={"title": book.title, "year": book.year, "edition": book.edition,
                           "price_count": book.price_count, "kept_for_estimates": kept})

    def create_new_edition(self, book_id: int, title: str, year: int, edition: str | None = None,
                           valid_from: str | None = None) -> int:
        """Kitabı kayıtlarıyla birlikte yeni bir TASLAK kitaba kopyalar; kaynak kitap değişmez."""
        src = self.get_book(book_id)
        title, year, edition, valid_from = self._validate_book(src.authority_id, title, year, edition, valid_from)
        with transaction(self.conn):
            new_id = price_books.insert(self.conn, src.authority_id, title, year, edition, valid_from,
                                        f"{src.display_name} kitabından kopyalandı.")
            copied = price_books.copy_prices(self.conn, book_id, new_id)
            rate_positions.copy(self.conn, book_id, new_id)
            audit.log(self.conn, "price_book", new_id, "TOPLU",
                      new={"source_book_id": book_id, "copied": copied}, note="Yeni sürüm oluşturuldu")
        return new_id

    # --- poz fiyat kayıtları ----------------------------------------------
    def _draft_book(self, book_id: int) -> PriceBook:
        book = self.get_book(book_id)
        if book.status != BookStatus.TASLAK:
            raise ConflictError("Poz kayıtları yalnızca taslak kitaplarda değiştirilebilir. "
                                "Aktif kitapta düzeltme için kitabı taslağa alın ya da yeni sürüm oluşturun.")
        return book

    def add_position_price(self, book_id: int, item: NewPositionPrice) -> int:
        return self.add_position_prices(book_id, [item])[0]

    def add_position_prices(self, book_id: int, items: Sequence[NewPositionPrice]) -> list[int]:
        book = self._draft_book(book_id)
        clean = [_validate_new_price(i) for i in items]
        seen = positions.codes_in_book(self.conn, book_id)
        for it in clean:
            key = normalize_code(it.code)
            if key in seen:
                raise ConflictError(f"'{it.code}' poz kodu bu kitapta zaten var.")
            seen.add(key)
        with transaction(self.conn):
            ids = positions.insert_prices(self.conn, book.authority_id, book_id, clean)
            if len(clean) == 1:
                audit.log(self.conn, "position_price", ids[0], "EKLE",
                          new={"code": clean[0].code, "unit_price": clean[0].unit_price})
            else:
                audit.log(self.conn, "price_book", book_id, "TOPLU", new={"added": len(ids)})
        return ids

    def update_position_price(self, price_id: int, description: str, unit: str, unit_price: Decimal,
                              category_path: str | None = None) -> None:
        old = self.get_price(price_id)
        self._draft_book(old.book_id)
        description, unit = _require(description, "Poz tanımı"), _require(unit, "Birim")
        unit_price = validate_price(unit_price)
        with transaction(self.conn):
            positions.update_price(self.conn, price_id, old.code, description, unit, unit_price,
                                   _clean(category_path))
            audit.log(self.conn, "position_price", price_id, "GUNCELLE",
                      old={"description": old.description, "unit": old.unit, "unit_price": old.unit_price},
                      new={"description": description, "unit": unit, "unit_price": unit_price})

    def delete_position_price(self, price_id: int) -> None:
        old = self.get_price(price_id)
        self._draft_book(old.book_id)
        if positions.reference_count(self.conn, price_id):
            raise ConflictError("Bu poz fiyatı projelerde ya da analizlerde kullanılıyor; silinemez.")
        with transaction(self.conn):
            positions.delete_price(self.conn, price_id)
            positions.delete_orphan_positions(self.conn)
            audit.log(self.conn, "position_price", price_id, "SIL",
                      old={"code": old.code, "book_id": old.book_id, "unit_price": old.unit_price})

    # --- arama ve geçmiş ---------------------------------------------------
    def get_price(self, price_id: int) -> PositionPrice:
        p = positions.get(self.conn, price_id)
        if p is None:
            raise NotFoundError("Poz fiyat kaydı bulunamadı.")
        return p

    def search(self, f: SearchFilter, offset: int = 0, limit: int = 500) -> list[PositionPrice]:
        return positions.search(self.conn, f, offset, limit)

    def count(self, f: SearchFilter) -> int:
        return positions.count(self.conn, f)

    def history(self, position_id: int) -> list[PriceHistoryEntry]:
        return positions.history(self.conn, position_id)

    def categories(self) -> list[str]:
        return positions.categories(self.conn)

    def years(self) -> list[int]:
        return positions.years(self.conn)
