-- Oran ve parametre tablosu: mevzuat, fiyat farkı ve endeks oranları kullanıcı tarafından, kaynağıyla (kanun/tebliğ/sayı,
-- tarih) girilir; program oran ÜRETMEZ ve önceden doldurmaz. Her oranın geçerlilik aralığı ve doğrulama işareti vardır.
CREATE TABLE ref_rates (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL CHECK (length(trim(key)) > 0),
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind        TEXT NOT NULL CHECK (kind IN ('MEVZUAT', 'FIYAT_FARKI', 'ENDEKS', 'DIGER')),
    value       TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '%',
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,
    source_ref  TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
    source_date TEXT,
    verified    INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    verified_by TEXT,
    verified_at TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX ix_ref_rates_key ON ref_rates(key, valid_from);
