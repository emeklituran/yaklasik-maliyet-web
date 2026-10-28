-- 0006: Endeks serileri ve aylık değerleri, yaklaşık maliyette endeks güncellemesi, yeni birim fiyat tespitleri.
-- Endeks DEĞERİ hazır gelmez; kullanıcı TÜİK yayınlarından girer ya da dosyadan alır.

CREATE TABLE index_series (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE CHECK (length(trim(code)) > 0),
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    source     TEXT NOT NULL DEFAULT '',
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE index_values (
    id         INTEGER PRIMARY KEY,
    series_id  INTEGER NOT NULL REFERENCES index_series(id) ON DELETE CASCADE,
    period     TEXT NOT NULL CHECK (period GLOB '[12][0-9][0-9][0-9]-[01][0-9]'),
    value      TEXT NOT NULL CHECK (value GLOB '[0-9]*' AND value NOT GLOB '*[^0-9.]*'),
    source     TEXT,
    entered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (series_id, period)
);

INSERT INTO index_series(code, name, source, notes)
VALUES ('YI-UFE', 'Yİ-ÜFE Genel Endeksi', 'TÜİK',
        'Alt endeks belirlenemeyen imalatlarda kullanılır (2003=100). Değerleri TÜİK yayınlarından girin.');

-- Keşif düzeyinde yaklaşık maliyet hesap tarihi ve varsayılan endeks; kalem düzeyinde endeks sınıfı.
ALTER TABLE estimates ADD COLUMN ym_date TEXT;
ALTER TABLE estimates ADD COLUMN index_series_id INTEGER REFERENCES index_series(id) ON DELETE SET NULL;
ALTER TABLE estimate_items ADD COLUMN index_series_id INTEGER REFERENCES index_series(id) ON DELETE SET NULL;

-- Sözleşme sürecinde yeni birim fiyat tespiti kayıtları (değişmez).
CREATE TABLE new_price_records (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id) ON DELETE RESTRICT,
    code         TEXT,
    description  TEXT NOT NULL CHECK (length(trim(description)) > 0),
    unit         TEXT,
    unit_price   TEXT NOT NULL CHECK (unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*'),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TRIGGER trg_new_price_no_update BEFORE UPDATE ON new_price_records
BEGIN
    SELECT RAISE(ABORT, 'Fiyat tespit kaydı değiştirilemez; yeni tespit yapın.');
END;

-- Yaklaşık maliyet profili: güncelliğini yitirmiş fiyatlar endeksle güncellenir.
INSERT INTO calc_profiles(kind, name, version, params_json, legal_basis_text, verified)
VALUES ('YAKLASIK', 'Yaklaşık maliyet — endeks güncellemeli', 1,
        '{"rounding":{"quantity_places":3,"price_places":2,"amount_places":2,"analysis_places":6,"mode":"HALF_UP"},"prices_include_overhead_profit":true,"overhead_profit_rate":"0.25","vat_rate":"0.20","index_update":true}',
        'DOĞRULANMAMIŞ PROFİL. Güncelliğini kaybeden birim fiyatlar: Birim fiyat × (Güncel endeks / Temel endeks). Güncel endeks: yaklaşık maliyet hesap tarihinden bir önceki ay; temel endeks: fiyatın yürürlüğe girdiği aydan bir önceki ay. Alt endeks belirlenemezse Yİ-ÜFE genel endeksi. Kaynak: hakedis.org "Yeni Mevzuata Göre Fiyat Tespiti" (YİİUY md.10 aktarımı; 1 Eylül 2025 sonrası ihaleler). Birim fiyatların %25 kâr ve genel gider içerdiği, KDV %20 (bilgi amaçlı) varsayılmıştır. Resmî metin ve ihale dokümanıyla doğrulayın.',
        0);
