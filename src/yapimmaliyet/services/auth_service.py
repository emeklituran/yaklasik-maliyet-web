"""Kullanıcılar, roller ve modül yetkileri (yapılandırılabilir, isteğe bağlı).

- Hiç kullanıcı yoksa uygulama kısıtsız çalışır; ilk oluşturulan kullanıcı Yönetici olmak zorundadır.
- Son etkin Yönetici silinemez, pasifleştirilemez ya da başka role geçirilemez (kilitlenmeyi önler).
- Kullanıcı/rol/yetki değişiklikleri Yönetim (AYAR) yetkisi ister ve kayıt altına alınır (audit_log).
- PIN, PBKDF2-HMAC-SHA256 (200.000 tur, kullanıcıya özel tuz) ile saklanır; düz metin tutulmaz.
Güvenlik sınırı: yerel SQLite dosyasıdır; bu bir işlem kontrolüdür, dosyaya doğrudan erişimi olan biri uygulamayı atlayabilir.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit
from yapimmaliyet.infra import authz
from yapimmaliyet.infra.authz import ADMIN, APPROVE, DELETE, EDIT, MODULES, VIEW, Session
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, require

ITERATIONS = 200_000
MIN_PIN = 4
ADMIN_ROLE = "YONETICI"
_ALL = {m: 0 for m in MODULES}


def _mat(**kw: int) -> dict[str, int]:
    return {**_ALL, **kw}


DEFAULT_ROLES: dict[str, tuple[str, dict[str, int]]] = {
    "YONETICI": ("Yönetici", {m: ADMIN for m in MODULES}),
    "PROJE_YONETICISI": ("Proje yöneticisi", _mat(PROJE=DELETE, FIRMA=DELETE, BIRIM_FIYAT=EDIT, KESIF=DELETE, TEKLIF=DELETE,
                                                  SOZLESME=DELETE, HAKEDIS=APPROVE, SATIN_ALMA=APPROVE, MALIYET=DELETE,
                                                  FINANS=VIEW, RAPOR=DELETE, VERI=EDIT)),
    "TEKNIK_OFIS": ("Teknik ofis", _mat(PROJE=EDIT, FIRMA=EDIT, BIRIM_FIYAT=DELETE, KESIF=DELETE, TEKLIF=EDIT, SOZLESME=EDIT,
                                        HAKEDIS=EDIT, SATIN_ALMA=VIEW, MALIYET=VIEW, RAPOR=EDIT)),
    "SATIN_ALMA": ("Satın alma", _mat(PROJE=VIEW, FIRMA=EDIT, BIRIM_FIYAT=VIEW, KESIF=VIEW, TEKLIF=EDIT, SOZLESME=VIEW,
                                      HAKEDIS=VIEW, SATIN_ALMA=DELETE, MALIYET=VIEW, RAPOR=EDIT)),
    "MUHASEBE": ("Muhasebe / finans", _mat(PROJE=VIEW, FIRMA=EDIT, TEKLIF=VIEW, SOZLESME=VIEW, HAKEDIS=APPROVE, SATIN_ALMA=VIEW,
                                          MALIYET=DELETE, FINANS=DELETE, RAPOR=EDIT)),
    "GORUNTULEME": ("Yalnız görüntüleme", _mat(**{m: VIEW for m in MODULES if m not in ("VERI", "AYAR")})),
}


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()


class AuthService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.ensure_defaults()

    def ensure_defaults(self) -> None:
        if self.conn.execute("SELECT 1 FROM app_roles LIMIT 1").fetchone():
            return
        with transaction(self.conn):
            for key, (name, perms) in DEFAULT_ROLES.items():
                self.conn.execute("INSERT INTO app_roles(key, name, is_system) VALUES (?, ?, 1)", (key, name))
                for module, level in perms.items():
                    self.conn.execute("INSERT INTO app_role_perms(role_key, module, level) VALUES (?, ?, ?)", (key, module, level))

    @staticmethod
    def _admin_only(what: str) -> None:
        authz.require("AYAR", ADMIN, what)

    # --- kullanıcılar ----------------------------------------------------------
    def has_users(self) -> bool:
        return self.conn.execute("SELECT 1 FROM app_users WHERE active = 1").fetchone() is not None

    def users(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT u.*, r.name AS role_name FROM app_users u JOIN app_roles r ON r.key = u.role_key "
                                 "ORDER BY u.username").fetchall()

    def _user(self, user_id: int) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM app_users WHERE id = ?", (user_id,)).fetchone()
        if r is None:
            raise NotFoundError("Kullanıcı bulunamadı.")
        return r

    def _admins_left(self, excluding: int | None = None) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM app_users WHERE active = 1 AND role_key = ? AND id IS NOT ?",
                                 (ADMIN_ROLE, excluding)).fetchone()[0]

    @staticmethod
    def _pin(pin: str) -> str:
        if len(pin or "") < MIN_PIN:
            raise ValidationError(f"PIN en az {MIN_PIN} karakter olmalıdır.")
        return pin

    def create_user(self, username: str, display_name: str, role_key: str, pin: str) -> int:
        if self.has_users():
            self._admin_only("kullanıcı ekleme")
        elif role_key != ADMIN_ROLE:
            raise ValidationError("İlk kullanıcı Yönetici olmalıdır; aksi hâlde kimse yetki veremez.")
        username = require(username, "Kullanıcı adı")
        if self.conn.execute("SELECT 1 FROM app_roles WHERE key = ?", (role_key,)).fetchone() is None:
            raise ValidationError("Geçersiz rol.")
        if self.conn.execute("SELECT 1 FROM app_users WHERE username = ?", (username,)).fetchone():
            raise ConflictError("Bu kullanıcı adı zaten var.")
        salt = secrets.token_hex(16)
        with transaction(self.conn):
            uid = self.conn.execute("INSERT INTO app_users(username, display_name, role_key, salt, pin_hash) VALUES (?, ?, ?, ?, ?)",
                                    (username, clean(display_name) or username, role_key, salt, _hash(self._pin(pin), salt))).lastrowid
            audit.log(self.conn, "app_user", uid, "EKLE", new={"username": username, "role": role_key})
        return uid

    def set_role(self, user_id: int, role_key: str) -> None:
        self._admin_only("rol değiştirme")
        u = self._user(user_id)
        if self.conn.execute("SELECT 1 FROM app_roles WHERE key = ?", (role_key,)).fetchone() is None:
            raise ValidationError("Geçersiz rol.")
        if u["role_key"] == ADMIN_ROLE and role_key != ADMIN_ROLE and self._admins_left(user_id) == 0:
            raise ConflictError("Son Yönetici başka role geçirilemez.")
        with transaction(self.conn):
            self.conn.execute("UPDATE app_users SET role_key = ? WHERE id = ?", (role_key, user_id))
            audit.log(self.conn, "app_user", user_id, "GUNCELLE", old={"role": u["role_key"]}, new={"role": role_key})

    def set_pin(self, user_id: int, pin: str) -> None:
        cur = authz.current()
        if cur is None or cur.user_id != user_id:
            self._admin_only("PIN değiştirme")
        self._user(user_id)
        salt = secrets.token_hex(16)
        with transaction(self.conn):
            self.conn.execute("UPDATE app_users SET salt = ?, pin_hash = ? WHERE id = ?", (salt, _hash(self._pin(pin), salt), user_id))
            audit.log(self.conn, "app_user", user_id, "GUNCELLE", note="PIN değiştirildi")

    def set_active(self, user_id: int, active: bool) -> None:
        self._admin_only("kullanıcı durumu")
        u = self._user(user_id)
        if not active and u["role_key"] == ADMIN_ROLE and self._admins_left(user_id) == 0:
            raise ConflictError("Son etkin Yönetici pasifleştirilemez.")
        with transaction(self.conn):
            self.conn.execute("UPDATE app_users SET active = ? WHERE id = ?", (int(active), user_id))
            audit.log(self.conn, "app_user", user_id, "GUNCELLE", new={"active": active})

    def delete_user(self, user_id: int) -> None:
        self._admin_only("kullanıcı silme")
        u = self._user(user_id)
        if u["role_key"] == ADMIN_ROLE and self._admins_left(user_id) == 0:
            raise ConflictError("Son Yönetici silinemez.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM app_users WHERE id = ?", (user_id,))
            audit.log(self.conn, "app_user", user_id, "SIL", old={"username": u["username"]})

    # --- roller ve yetkiler ----------------------------------------------------
    def roles(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM app_roles ORDER BY is_system DESC, name").fetchall()

    def role_perms(self, role_key: str) -> dict[str, int]:
        perms = {m: 0 for m in MODULES}
        perms.update({r["module"]: r["level"] for r in self.conn.execute(
            "SELECT module, level FROM app_role_perms WHERE role_key = ?", (role_key,))})
        return perms

    def set_perm(self, role_key: str, module: str, level: int) -> None:
        self._admin_only("yetki değiştirme")
        if module not in MODULES or not 0 <= level <= ADMIN:
            raise ValidationError("Geçersiz modül ya da yetki düzeyi.")
        if role_key == ADMIN_ROLE and module == "AYAR" and level < ADMIN:
            raise ConflictError("Yönetici rolünün Ayarlar yönetim yetkisi düşürülemez.")
        old = self.role_perms(role_key).get(module, 0)
        with transaction(self.conn):
            self.conn.execute("INSERT INTO app_role_perms(role_key, module, level) VALUES (?, ?, ?) ON CONFLICT(role_key, module)"
                              " DO UPDATE SET level = excluded.level", (role_key, module, level))
            audit.log(self.conn, "app_role", None, "GUNCELLE", old={"role": role_key, "module": module, "level": old},
                      new={"role": role_key, "module": module, "level": level}, note="Yetki değişikliği")

    def create_role(self, key: str, name: str, copy_from: str = "GORUNTULEME") -> None:
        self._admin_only("rol ekleme")
        key = require(key, "Rol anahtarı").upper().replace(" ", "_")
        if self.conn.execute("SELECT 1 FROM app_roles WHERE key = ?", (key,)).fetchone():
            raise ConflictError("Bu rol zaten var.")
        perms = self.role_perms(copy_from)
        with transaction(self.conn):
            self.conn.execute("INSERT INTO app_roles(key, name, is_system) VALUES (?, ?, 0)", (key, require(name, "Rol adı")))
            for m, lv in perms.items():
                self.conn.execute("INSERT INTO app_role_perms(role_key, module, level) VALUES (?, ?, ?)", (key, m, lv))
            audit.log(self.conn, "app_role", None, "EKLE", new={"role": key, "copy_from": copy_from})

    def delete_role(self, key: str) -> None:
        self._admin_only("rol silme")
        r = self.conn.execute("SELECT * FROM app_roles WHERE key = ?", (key,)).fetchone()
        if r is None:
            raise NotFoundError("Rol bulunamadı.")
        if r["is_system"]:
            raise ConflictError("Hazır roller silinemez; yetkileri değiştirilebilir.")
        if self.conn.execute("SELECT 1 FROM app_users WHERE role_key = ?", (key,)).fetchone():
            raise ConflictError("Bu role bağlı kullanıcı var; önce kullanıcıların rolünü değiştirin.")
        with transaction(self.conn):
            self.conn.execute("DELETE FROM app_roles WHERE key = ?", (key,))
            audit.log(self.conn, "app_role", None, "SIL", old={"role": key})

    # --- oturum ----------------------------------------------------------------
    def session_for(self, user_id: int) -> Session:
        u = self._user(user_id)
        return Session(u["id"], u["username"], u["display_name"], u["role_key"], self.role_perms(u["role_key"]))

    def login(self, username: str, pin: str) -> Session:
        u = self.conn.execute("SELECT * FROM app_users WHERE username = ? AND active = 1", ((username or "").strip(),)).fetchone()
        if u is None or not hmac.compare_digest(_hash(pin or "", u["salt"]), u["pin_hash"]):
            raise ValidationError("Kullanıcı adı ya da PIN hatalı.")
        return self.session_for(u["id"])

    def activate(self, session: Session | None) -> None:
        authz.activate(session)
