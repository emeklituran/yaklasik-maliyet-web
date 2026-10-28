"""Ortak taraf (firma) yönetimi: ekleme, düzenleme, rol, birleştirme, pasifleştirme.

Belgelerdeki firma adı metinleri değiştirilmez (o belgede nasıl yazıldıysa öyle kalır); taraf bağı ayrıca tutulur.
Bir firma yeniden adlandırılırsa ya da birleştirilirse eski yazımı takma ad olarak saklanır; aynı firma ikinci kez
ayrı kayıt olarak açılmaz.
"""

from __future__ import annotations

import re
import sqlite3

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.db.repositories import parties as repo
from yapimmaliyet.db.repositories.parties import DETAIL_FIELDS, ROLES, Party
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import require

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def link_existing(conn: sqlite3.Connection) -> int:
    """Açılışta: taraf bağı olmayan belge satırlarını bağlar (geçiş sonrası ve yedekten dönüşte)."""
    with transaction(conn):
        return repo.link_unlinked(conn)


class PartyService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def parties(self, role: str | None = None, include_inactive: bool = False) -> list[Party]:
        return repo.list_parties(self.conn, role, include_inactive)

    def names(self, role: str | None = None) -> list[str]:
        """Seçim listeleri için etkin firma adları (role göre önce, sonra diğerleri)."""
        all_ = self.parties()
        first = [p.name for p in all_ if role and role in p.roles]
        return first + [p.name for p in all_ if p.name not in first]

    def party(self, party_id: int) -> Party:
        p = repo.get(self.conn, party_id)
        if p is None:
            raise NotFoundError("Firma bulunamadı.")
        return p

    def usage(self, party_id: int) -> list[tuple[str, int]]:
        return repo.usage(self.conn, party_id)

    def usage_counts(self) -> dict[int, int]:
        return repo.usage_counts(self.conn)

    def _clean(self, name: str, details: dict, roles: list[str]) -> tuple[str, dict, list[str]]:
        name = require(name, "Firma adı")
        if not repo.name_key(name):
            raise ValidationError("Firma adı yalnızca noktalama işaretlerinden oluşamaz.")
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValidationError(f"Geçersiz rol: {', '.join(bad)}")
        d = {f: (details.get(f) or "").strip() for f in DETAIL_FIELDS}
        if d["email"] and not _EMAIL.match(d["email"]):
            raise ValidationError("E-posta adresi geçersiz.")
        if d["tax_no"] and not (d["tax_no"].isdigit() and len(d["tax_no"]) in (10, 11)):
            raise ValidationError("Vergi / T.C. kimlik no 10 ya da 11 haneli rakam olmalıdır.")
        return name, d, roles

    def create(self, name: str, roles: list[str], details: dict | None = None) -> int:
        name, d, roles = self._clean(name, details or {}, roles)
        if repo.find(self.conn, name) is not None:
            raise ConflictError(f"'{name}' adında (ya da bu yazımla eşleşen) bir firma zaten var.")
        with transaction(self.conn):
            pid = repo.insert(self.conn, name, d)
            repo.set_roles(self.conn, pid, roles)
            audit.log(self.conn, "party", pid, "EKLE", new={"name": name, "roles": roles})
        return pid

    def update(self, party_id: int, name: str, roles: list[str], details: dict) -> None:
        old = self.party(party_id)
        name, d, roles = self._clean(name, details, roles)
        other = repo.find(self.conn, name)
        if other not in (None, party_id):
            raise ConflictError(f"'{name}' adı başka bir firma kaydıyla eşleşiyor; gerekirse iki kaydı birleştirin.")
        old_key, new_key = repo.name_key(old.name), repo.name_key(name)
        with transaction(self.conn):
            if old_key != new_key:
                repo.drop_alias(self.conn, new_key)
                repo.add_alias(self.conn, old_key, party_id)   # eski yazımla gelen belgeler aynı firmaya bağlanır
            repo.update(self.conn, party_id, name, d)
            repo.set_roles(self.conn, party_id, roles)
            audit.log(self.conn, "party", party_id, "GUNCELLE", old={"name": old.name, "roles": old.roles},
                      new={"name": name, "roles": roles})

    def set_active(self, party_id: int, active: bool) -> None:
        self.party(party_id)
        with transaction(self.conn):
            repo.set_active(self.conn, party_id, active)
            audit.log(self.conn, "party", party_id, "GUNCELLE", new={"active": active})

    def delete(self, party_id: int) -> None:
        p = self.party(party_id)
        used = self.usage(party_id)
        if used:
            detail = ", ".join(f"{label}: {n}" for label, n in used)
            raise ConflictError(f"Bu firma belgelerde kullanılıyor ({detail}); silinemez. "
                                "Listede görünmemesi için pasifleştirebilirsiniz.")
        with transaction(self.conn):
            repo.delete(self.conn, party_id)
            audit.log(self.conn, "party", party_id, "SIL", old={"name": p.name})

    def merge(self, keep_id: int, drop_id: int) -> None:
        """İki kaydı birleştirir: belgeler, roller ve adlar `keep_id` kaydına geçer; boş bilgiler tamamlanır."""
        if keep_id == drop_id:
            raise ValidationError("Bir firma kendisiyle birleştirilemez.")
        keep, drop = self.party(keep_id), self.party(drop_id)
        details = {f: getattr(keep, f) or getattr(drop, f) for f in DETAIL_FIELDS}
        with transaction(self.conn):
            repo.repoint(self.conn, drop_id, keep_id)
            repo.delete(self.conn, drop_id)
            repo.add_alias(self.conn, repo.name_key(drop.name), keep_id)
            repo.update(self.conn, keep_id, keep.name, details)
            if not drop.is_sample:
                self.conn.execute("UPDATE parties SET is_sample = 0 WHERE id = ?", (keep_id,))
            audit.log(self.conn, "party", keep_id, "TOPLU", old={"merged": drop.name, "merged_id": drop_id},
                      new={"name": keep.name}, note="Firma kayıtları birleştirildi")
