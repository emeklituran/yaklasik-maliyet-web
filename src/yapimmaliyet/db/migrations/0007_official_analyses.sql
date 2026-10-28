-- 0007: Resmî birim fiyat ANALİZ kitapları (ÇŞB inşaat, mekanik, elektrik analizleri).
-- Analizler birim fiyat listesinden ayrı tutulur; positions / position_prices / price_books tablolarına
-- hiçbir bağımlılık ya da yazma yoktur. Fiyat karşılaştırması yalnızca poz kodu (code_norm) üzerinden okunur.
-- Ondalıklar kanonik TEXT; bileşen miktarı ve tutarı negatif olabilir ("minha" satırları: -0,05 × 458,72).

CREATE TABLE analysis_books (
    id             INTEGER PRIMARY KEY,
    authority_id   INTEGER NOT NULL REFERENCES authorities(id) ON DELETE RESTRICT,
    title          TEXT NOT NULL CHECK (length(trim(title)) > 0),
    year           INTEGER NOT NULL CHECK (year BETWEEN 1950 AND 2200),
    edition        TEXT NOT NULL DEFAULT '',
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE RESTRICT,
    import_job_id  INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (authority_id, year, edition, title)
);

CREATE TABLE official_analyses (
    id               INTEGER PRIMARY KEY,
    analysis_book_id INTEGER NOT NULL REFERENCES analysis_books(id) ON DELETE CASCADE,
    code             TEXT NOT NULL CHECK (length(trim(code)) > 0),
    code_norm        TEXT NOT NULL CHECK (length(code_norm) > 0),
    name             TEXT NOT NULL DEFAULT '',
    unit             TEXT,
    analysis_date    TEXT CHECK (analysis_date IS NULL OR analysis_date GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    group_label      TEXT,   -- 'MALZEME_MONTAJ': malzeme/montaj ayrımlı (mekanik, elektrik); ayrım yoksa NULL
    subtotal         TEXT CHECK (subtotal IS NULL OR (subtotal GLOB '[0-9]*' AND subtotal NOT GLOB '*[^0-9.]*')),
    material_total   TEXT CHECK (material_total IS NULL OR (material_total GLOB '[0-9]*' AND material_total NOT GLOB '*[^0-9.]*')),
    labour_total     TEXT CHECK (labour_total IS NULL OR (labour_total GLOB '[0-9]*' AND labour_total NOT GLOB '*[^0-9.]*')),
    overhead_rate    TEXT CHECK (overhead_rate IS NULL OR (overhead_rate GLOB '[0-9]*' AND overhead_rate NOT GLOB '*[^0-9.]*')),
    overhead_amount  TEXT CHECK (overhead_amount IS NULL OR (overhead_amount GLOB '[0-9]*' AND overhead_amount NOT GLOB '*[^0-9.]*')),
    unit_price       TEXT CHECK (unit_price IS NULL OR (unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*')),
    montage_price    TEXT CHECK (montage_price IS NULL OR (montage_price GLOB '[0-9]*' AND montage_price NOT GLOB '*[^0-9.]*')),
    notes            TEXT,   -- tarif / ölçü / not metni
    page             INTEGER CHECK (page IS NULL OR page >= 1),
    check_ok         INTEGER NOT NULL DEFAULT 0 CHECK (check_ok IN (0, 1)),   -- aritmetik tutarlılık
    warnings_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings_json)),
    UNIQUE (analysis_book_id, code_norm)
);
CREATE INDEX ix_official_analyses_code_norm ON official_analyses(code_norm);

CREATE TABLE official_analysis_items (
    id            INTEGER PRIMARY KEY,
    analysis_id   INTEGER NOT NULL REFERENCES official_analyses(id) ON DELETE CASCADE,
    line_no       INTEGER NOT NULL CHECK (line_no >= 1),
    section       TEXT,   -- "Malzeme", "Montaj" ya da alt başlık metni ("Foraj:", "Yükleme, boşaltma ...")
    code          TEXT,
    description   TEXT NOT NULL DEFAULT '',
    unit          TEXT,
    quantity      TEXT CHECK (quantity IS NULL OR (ltrim(quantity, '-') GLOB '[0-9]*'
                  AND ltrim(quantity, '-') NOT GLOB '*[^0-9.]*' AND length(quantity) - length(ltrim(quantity, '-')) <= 1)),
    quantity_text TEXT,   -- kaynaktaki yazım ("21/4,50" gibi kesirli miktarlar)
    unit_price    TEXT CHECK (unit_price IS NULL OR (ltrim(unit_price, '-') GLOB '[0-9]*'
                  AND ltrim(unit_price, '-') NOT GLOB '*[^0-9.]*' AND length(unit_price) - length(ltrim(unit_price, '-')) <= 1)),
    amount        TEXT CHECK (amount IS NULL OR (ltrim(amount, '-') GLOB '[0-9]*'
                  AND ltrim(amount, '-') NOT GLOB '*[^0-9.]*' AND length(amount) - length(ltrim(amount, '-')) <= 1)),
    amount_column TEXT CHECK (amount_column IS NULL OR amount_column IN ('Malzeme', 'Montaj')),
    UNIQUE (analysis_id, line_no)
);
CREATE INDEX ix_official_analysis_items_code ON official_analysis_items(code);
