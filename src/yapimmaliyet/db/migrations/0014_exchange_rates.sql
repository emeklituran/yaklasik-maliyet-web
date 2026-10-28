-- 0014: Döviz kurları. TL temel para birimidir; kur = 1 birim dövizin TL karşılığı (TCMB'de birim 100 olan
-- dövizler 1 birime bölünerek saklanır, ham değer ve birim ayrıca tutulur).
-- Kur kayıtları değiştirilemez: düzeltme gerekiyorsa yeni kayıt eklenir. Bir keşfe sabitlenen kur, sonradan
-- çekilen güncel kurlardan etkilenmez; kesinleşen hesap kaydı (snapshot) kullanılan kuru ayrıca içerir.

CREATE TABLE exchange_rates (
    id          INTEGER PRIMARY KEY,
    currency    TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency) AND currency <> 'TRY'),
    rate        TEXT NOT NULL CHECK (rate GLOB '[0-9]*' AND rate NOT GLOB '*[^0-9.]*' AND CAST(rate AS REAL) > 0),
    raw_rate    TEXT,
    unit        INTEGER NOT NULL DEFAULT 1 CHECK (unit >= 1),
    rate_type   TEXT NOT NULL CHECK (rate_type IN ('DOVIZ_ALIS', 'DOVIZ_SATIS', 'EFEKTIF_ALIS', 'EFEKTIF_SATIS', 'MANUEL')),
    rate_date   TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('TCMB', 'MANUEL')),
    source_url  TEXT,
    bulletin_no TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    note        TEXT,
    UNIQUE (currency, rate_type, rate_date, source)
);
CREATE INDEX ix_exchange_rates_lookup ON exchange_rates(currency, rate_date);

CREATE TRIGGER trg_exchange_rates_no_update BEFORE UPDATE ON exchange_rates BEGIN
    SELECT RAISE(ABORT, 'Kur kaydı değiştirilemez; yeni kayıt ekleyin.');
END;

ALTER TABLE estimates ADD COLUMN fx_rate_id INTEGER REFERENCES exchange_rates(id) ON DELETE RESTRICT;
