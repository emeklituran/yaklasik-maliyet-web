"""Birim fiyat kitapları."""

from __future__ import annotations

import sqlite3

from yapimmaliyet.domain.models import BookStatus, PriceBook

_SELECT = (
    "SELECT pb.*, a.name AS authority_name, a.code AS authority_code,"
    " (SELECT COUNT(*) FROM position_prices pp WHERE pp.price_book_id = pb.id) AS price_count"
    " FROM price_books pb JOIN authorities a ON a.id = pb.authority_id"
)
HIDE = " pb.hidden = 0"


def _row(r: sqlite3.Row) -> PriceBook:
    return PriceBook(r["id"], r["authority_id"], r["authority_name"], r["title"], r["year"],
                     r["edition"], r["valid_from"], BookStatus(r["status"]), r["notes"],
                     r["price_count"], r["discipline"], r["period"], r["valid_to"], r["published_on"],
                     r["revision_no"], r["revised_on"], r["price_kind"], r["verification"], r["source_ref"],
                     r["authority_code"])


def list_books(conn: sqlite3.Connection, authority_id: int | None = None) -> list[PriceBook]:
    sql, params = _SELECT + " WHERE" + HIDE, []
    if authority_id is not None:
        sql += " AND pb.authority_id = ?"
        params.append(authority_id)
    sql += " ORDER BY a.name, pb.year DESC, pb.edition"
    return [_row(r) for r in conn.execute(sql, params)]


def get(conn: sqlite3.Connection, book_id: int) -> PriceBook | None:
    r = conn.execute(_SELECT + " WHERE pb.id = ?", (book_id,)).fetchone()
    return _row(r) if r else None


def exists(conn: sqlite3.Connection, authority_id: int, year: int, edition: str,
           exclude_id: int | None = None) -> bool:
    r = conn.execute(
        "SELECT 1 FROM price_books WHERE authority_id = ? AND year = ? AND edition = ? AND id IS NOT ?",
        (authority_id, year, edition, exclude_id)).fetchone()
    return r is not None


def insert(conn: sqlite3.Connection, authority_id: int, title: str, year: int, edition: str,
           valid_from: str | None, notes: str | None) -> int:
    return conn.execute(
        "INSERT INTO price_books(authority_id, title, year, edition, valid_from, notes)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (authority_id, title, year, edition, valid_from, notes)).lastrowid


def update(conn: sqlite3.Connection, book_id: int, title: str, year: int, edition: str,
           valid_from: str | None, notes: str | None) -> None:
    conn.execute(
        "UPDATE price_books SET title = ?, year = ?, edition = ?, valid_from = ?, notes = ? WHERE id = ?",
        (title, year, edition, valid_from, notes, book_id))


META_FIELDS = ("period", "valid_to", "published_on", "revision_no", "revised_on", "price_kind", "verification",
               "source_ref")


def set_meta(conn: sqlite3.Connection, book_id: int, **meta) -> None:
    """Dönem, geçerlilik bitişi, yayın/revizyon, fiyat türü, doğrulama ve kaynak alanları (yalnız verilenler)."""
    fields = {k: v for k, v in meta.items() if k in META_FIELDS}   # sütun adları sabit listeden
    if fields:
        conn.execute(f"UPDATE price_books SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), book_id))


def set_status(conn: sqlite3.Connection, book_id: int, status: BookStatus) -> None:
    conn.execute("UPDATE price_books SET status = ? WHERE id = ?", (status.value, book_id))


def reference_count(conn: sqlite3.Connection, book_id: int) -> int:
    """Kitaptaki fiyatlara keşif kalemlerinden ve analiz girdilerinden yapılan başvuru sayısı."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM estimate_items ei JOIN position_prices pp ON pp.id = ei.position_price_id"
        "        WHERE pp.price_book_id = ?)"
        " + (SELECT COUNT(*) FROM analysis_items ai JOIN position_prices pp ON pp.id = ai.ref_position_price_id"
        "        WHERE pp.price_book_id = ?)",
        (book_id, book_id)).fetchone()
    return row[0]


def keep_referenced_prices(conn: sqlite3.Connection, book_id: int) -> int:
    """Kitap silinirken keşif / analizlerin başvurduğu poz kayıtlarını kullanıcıya görünmeyen saklama kitabına taşır.

    Keşif kalemleri zaten kendi kopyasını taşır; bu taşıma yalnız başvuru bütünlüğünü (yabancı anahtar) korur. Saklama
    kitabı idare başına bir tanedir, listelerde, aramada ve eşleştirmede görünmez."""
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT pp.id FROM position_prices pp WHERE pp.price_book_id = ? AND (EXISTS (SELECT 1 FROM estimate_items"
        " ei WHERE ei.position_price_id = pp.id) OR EXISTS (SELECT 1 FROM analysis_items ai"
        " WHERE ai.ref_position_price_id = pp.id))", (book_id,))]
    if not ids:
        return 0
    authority = conn.execute("SELECT authority_id FROM price_books WHERE id = ?", (book_id,)).fetchone()[0]
    holder = conn.execute("SELECT id FROM price_books WHERE hidden = 1 AND authority_id = ?", (authority,)).fetchone()
    holder_id = holder[0] if holder else conn.execute(
        "INSERT INTO price_books(authority_id, title, year, edition, status, hidden, notes) VALUES (?, ?, ?, ?, 'TASLAK', 1, ?)",
        (authority, "Silinen kitaplardan korunan kayıtlar", 1950, "", "Sistem kaydı; kullanıcıya gösterilmez.")).lastrowid
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        conn.execute(f"UPDATE position_prices SET price_book_id = ? WHERE id IN ({','.join('?' * len(part))})",
                     (holder_id, *part))
    return len(ids)


def delete_with_prices(conn: sqlite3.Connection, book_id: int) -> None:
    conn.execute("DELETE FROM position_prices WHERE price_book_id = ?", (book_id,))
    conn.execute("DELETE FROM price_books WHERE id = ?", (book_id,))


def copy_prices(conn: sqlite3.Connection, src_book_id: int, dst_book_id: int, only_missing: bool = False) -> int:
    """Kaynak kitabın kayıtlarını hedefe kopyalar. `only_missing`: yalnızca hedefte olmayan pozlar."""
    sql = ("INSERT INTO position_prices(price_book_id, position_id, description, unit, unit_norm, unit_price,"
           " currency, search_text, source_page, source_row, raw_text, ocr_confidence, montage_price)"
           " SELECT ?, position_id, description, unit, unit_norm, unit_price, currency, search_text,"
           " source_page, source_row, raw_text, ocr_confidence, montage_price"
           " FROM position_prices WHERE price_book_id = ?")
    params: tuple = (dst_book_id, src_book_id)
    if only_missing:
        sql += " AND position_id NOT IN (SELECT position_id FROM position_prices WHERE price_book_id = ?)"
        params += (dst_book_id,)
    return conn.execute(sql, params).rowcount


def prices_by_code(conn: sqlite3.Connection, book_id: int) -> dict[str, sqlite3.Row]:
    """Kitabın kayıtları, normalize poz koduna göre (code, description, unit, unit_norm, unit_price, montage_price)."""
    rows = conn.execute(
        "SELECT p.code_norm, p.code, pp.description, pp.unit, pp.unit_norm, pp.unit_price, pp.montage_price"
        " FROM position_prices pp JOIN positions p ON p.id = pp.position_id WHERE pp.price_book_id = ?", (book_id,))
    return {r["code_norm"]: r for r in rows}


def set_discipline(conn: sqlite3.Connection, book_id: int, discipline: str | None) -> None:
    conn.execute("UPDATE price_books SET discipline = ? WHERE id = ?", (discipline, book_id))
