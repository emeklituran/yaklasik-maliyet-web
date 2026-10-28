"""Ortak taraf (firma) kaydı ve belgelerdeki firma adlarının taraflara bağlanması."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from yapimmaliyet.domain.text_tr import normalize_search

ROLES = {"ISVEREN": "İşveren / İdare", "YUKLENICI": "Yüklenici", "TASERON": "Taşeron",
         "ISTEKLI": "İstekli", "TEDARIKCI": "Tedarikçi"}
DETAIL_FIELDS = ("short_name", "tax_office", "tax_no", "address", "phone", "email", "contact", "notes")

# Firma adı tutan belge sütunları: (tablo, ad sütunu, bağ sütunu, rol ifadesi, örnek bayrağı ifadesi, açıklama)
LINKS = (
    ("pp_contracts", "employer", "employer_party_id", "'ISVEREN'", "is_sample", "Hakediş sözleşmesi (işveren)"),
    ("pp_contracts", "contractor", "contractor_party_id",
     "CASE kind WHEN 'TASERON' THEN 'TASERON' ELSE 'YUKLENICI' END", "is_sample", "Hakediş sözleşmesi (yüklenici)"),
    ("tender_bids", "bidder", "party_id", "'ISTEKLI'", "is_sample", "İhale teklifi"),
    ("purchase_offers", "firm", "party_id", "'TEDARIKCI'", "0", "Satın alma teklifi"),
)
# Yalnız taraf bağıyla tutulan kayıtlar (ad metni yok): kullanım sayısı ve birleştirmede dikkate alınır
ID_LINKS = (("cari_entries", "party_id", "Cari hareket"), ("fin_notes", "party_id", "Çek / senet"))


def _all_links(conn: sqlite3.Connection):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return [(t, link, label) for t, _c, link, _r, _s, label in LINKS] + [x for x in ID_LINKS if x[0] in names]


def name_key(name: str) -> str:
    """Eşleştirme anahtarı: büyük/küçük harf, Türkçe harf, noktalama ve boşluk farkları yok sayılır
    ("A.Ş." = "AŞ" = "A. Ş."). Anahtar yalnızca harf ve rakamlardan oluşur (şema 0019)."""
    return normalize_search(name or "").replace(" ", "")


@dataclass
class Party:
    id: int
    name: str
    roles: list[str] = field(default_factory=list)
    short_name: str = ""
    tax_office: str = ""
    tax_no: str = ""
    address: str = ""
    phone: str = ""
    email: str = ""
    contact: str = ""
    notes: str = ""
    active: bool = True
    is_sample: bool = False

    @property
    def role_labels(self) -> str:
        return ", ".join(ROLES[r] for r in ROLES if r in self.roles)


def _roles(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for r in conn.execute("SELECT party_id, role FROM party_roles"):
        out.setdefault(r["party_id"], []).append(r["role"])
    return out


def _row(r: sqlite3.Row, roles: list[str]) -> Party:
    return Party(r["id"], r["name"], sorted(roles, key=list(ROLES).index),
                 **{f: r[f] for f in DETAIL_FIELDS}, active=bool(r["active"]), is_sample=bool(r["is_sample"]))


def list_parties(conn: sqlite3.Connection, role: str | None = None, include_inactive: bool = False) -> list[Party]:
    roles = _roles(conn)
    rows = conn.execute("SELECT * FROM parties" + ("" if include_inactive else " WHERE active = 1"))
    parties = [_row(r, roles.get(r["id"], [])) for r in rows]
    if role:
        parties = [p for p in parties if role in p.roles]
    return sorted(parties, key=lambda p: name_key(p.name))


def get(conn: sqlite3.Connection, party_id: int) -> Party | None:
    r = conn.execute("SELECT * FROM parties WHERE id = ?", (party_id,)).fetchone()
    return _row(r, _roles(conn).get(party_id, [])) if r else None


def find(conn: sqlite3.Connection, name: str) -> int | None:
    key = name_key(name)
    r = conn.execute("SELECT id FROM parties WHERE name_key = ? UNION ALL "
                     "SELECT party_id FROM party_aliases WHERE name_key = ? LIMIT 1", (key, key)).fetchone()
    return r[0] if r else None


def insert(conn: sqlite3.Connection, name: str, details: dict | None = None, is_sample: bool = False) -> int:
    d = {f: (details or {}).get(f, "") or "" for f in DETAIL_FIELDS}
    cols = ["name", "name_key", "is_sample", *d]
    return conn.execute(f"INSERT INTO parties({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        (name.strip(), name_key(name), int(is_sample), *d.values())).lastrowid


def update(conn: sqlite3.Connection, party_id: int, name: str, details: dict) -> None:
    d = {f: details.get(f, "") or "" for f in DETAIL_FIELDS}
    conn.execute(f"UPDATE parties SET name = ?, name_key = ?, {', '.join(f + ' = ?' for f in d)} WHERE id = ?",
                 (name.strip(), name_key(name), *d.values(), party_id))


def add_alias(conn: sqlite3.Connection, key: str, party_id: int) -> None:
    conn.execute("INSERT INTO party_aliases(name_key, party_id) VALUES (?, ?) "
                 "ON CONFLICT(name_key) DO UPDATE SET party_id = excluded.party_id", (key, party_id))


def drop_alias(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM party_aliases WHERE name_key = ?", (key,))


def set_roles(conn: sqlite3.Connection, party_id: int, roles: list[str]) -> None:
    conn.execute("DELETE FROM party_roles WHERE party_id = ?", (party_id,))
    add_roles(conn, party_id, roles)


def add_roles(conn: sqlite3.Connection, party_id: int, roles: list[str]) -> None:
    conn.executemany("INSERT OR IGNORE INTO party_roles(party_id, role) VALUES (?, ?)",
                     [(party_id, r) for r in roles])


def set_active(conn: sqlite3.Connection, party_id: int, active: bool) -> None:
    conn.execute("UPDATE parties SET active = ? WHERE id = ?", (int(active), party_id))


def delete(conn: sqlite3.Connection, party_id: int) -> None:
    conn.execute("DELETE FROM parties WHERE id = ?", (party_id,))


def resolve(conn: sqlite3.Connection, name: str, role: str, is_sample: bool = False) -> int | None:
    """Ada karşılık gelen tarafı bulur, yoksa açar; rolü ekler. Gerçek bir belge örnek bayrağını kaldırır."""
    if not (name or "").strip() or not name_key(name):
        return None
    pid = find(conn, name)
    if pid is None:
        pid = insert(conn, name, is_sample=is_sample)
    elif not is_sample:
        conn.execute("UPDATE parties SET is_sample = 0 WHERE id = ? AND is_sample = 1", (pid,))
    add_roles(conn, pid, [role])
    return pid


def link_unlinked(conn: sqlite3.Connection) -> int:
    """Taraf bağı boş olan belge satırlarını adlarına göre bağlar; bağlanan satır sayısını döndürür.

    Yazma transaction'ı içinde çağrılmalıdır (kendi transaction'ını açmaz).
    """
    count = 0
    for table, col, link, role_sql, sample_sql, _label in LINKS:
        rows = conn.execute(f"SELECT id, {col} AS name, {role_sql} AS role, {sample_sql} AS sample FROM {table} "
                            f"WHERE {link} IS NULL").fetchall()
        for r in rows:
            pid = resolve(conn, r["name"], r["role"], bool(r["sample"]))
            if pid is not None:
                conn.execute(f"UPDATE {table} SET {link} = ? WHERE id = ?", (pid, r["id"]))
                count += 1
    return count


def usage(conn: sqlite3.Connection, party_id: int) -> list[tuple[str, int]]:
    """Tarafın kullanıldığı belge türleri ve sayıları (sıfır olanlar dahil değil)."""
    out = []
    for table, link, label in _all_links(conn):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {link} = ?", (party_id,)).fetchone()[0]
        if n:
            out.append((label, n))
    return out


def usage_counts(conn: sqlite3.Connection) -> dict[int, int]:
    out: dict[int, int] = {}
    for table, link, _label in _all_links(conn):
        for r in conn.execute(f"SELECT {link} AS pid, COUNT(*) AS n FROM {table} WHERE {link} IS NOT NULL "
                              f"GROUP BY {link}"):
            out[r["pid"]] = out.get(r["pid"], 0) + r["n"]
    return out


def repoint(conn: sqlite3.Connection, from_id: int, to_id: int) -> None:
    """Birleştirme: belge bağlarını, rolleri ve eski adları hedef tarafa taşır."""
    for table, link, _label in _all_links(conn):
        conn.execute(f"UPDATE {table} SET {link} = ? WHERE {link} = ?", (to_id, from_id))
    conn.execute("INSERT OR IGNORE INTO party_roles(party_id, role) SELECT ?, role FROM party_roles WHERE party_id = ?",
                 (to_id, from_id))
    conn.execute("UPDATE party_aliases SET party_id = ? WHERE party_id = ?", (to_id, from_id))
