-- Kullanıcılar, roller ve modül bazlı yetkiler (isteğe bağlı).
-- Yetki düzeyi sayıdır ve kümülatiftir: 1 Görüntüleme, 2 Oluşturma, 3 Düzenleme, 4 Onaylama, 5 Silme/İptal, 6 Yönetim.
-- Hiç kullanıcı tanımlı değilse uygulama eskisi gibi kısıtsız çalışır (mevcut veriye erişim engellenmez).
-- Yerel bir SQLite dosyası olduğundan bu bir ERİŞİM KONTROLÜ değil, yanlışlıkla işlemi önleyen bir İŞLEM KONTROLÜDÜR:
-- veritabanı dosyasına doğrudan erişimi olan biri uygulamayı atlayabilir.

CREATE TABLE app_roles (
    key       TEXT PRIMARY KEY,
    name      TEXT NOT NULL CHECK (length(trim(name)) > 0),
    is_system INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1))
);

CREATE TABLE app_role_perms (
    role_key TEXT NOT NULL REFERENCES app_roles(key) ON DELETE CASCADE,
    module   TEXT NOT NULL,
    level    INTEGER NOT NULL CHECK (level BETWEEN 0 AND 6),
    PRIMARY KEY (role_key, module)
);

CREATE TABLE app_users (
    id           INTEGER PRIMARY KEY,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(username)) > 0),
    display_name TEXT NOT NULL DEFAULT '',
    role_key     TEXT NOT NULL REFERENCES app_roles(key) ON DELETE RESTRICT,
    salt         TEXT NOT NULL DEFAULT '',
    pin_hash     TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
