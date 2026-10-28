-- 0001: İlk şema (Aşama 1 teknik tasarımı §5).
-- Ondalık değerler TEXT olarak kanonik biçimde saklanır ("1234.5600").
-- Negatif olmayan ondalık kontrolü: x GLOB '[0-9]*' AND x NOT GLOB '*[^0-9.]*'
-- Bu dosya uygulandıktan sonra DEĞİŞTİRİLMEZ; değişiklikler yeni migration ile yapılır.

CREATE TABLE app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE authorities (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    short_name TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE source_files (
    id            INTEGER PRIMARY KEY,
    original_name TEXT NOT NULL,
    sha256        TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    stored_path   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes >= 0),
    imported_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE import_jobs (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('FIYAT_KITABI','KESIF')),
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE RESTRICT,
    status         TEXT NOT NULL CHECK (status IN ('HAZIRLANIYOR','ONAY_BEKLIYOR','TAMAMLANDI','IPTAL','HATA')),
    mapping_json   TEXT CHECK (mapping_json IS NULL OR json_valid(mapping_json)),
    stats_json     TEXT CHECK (stats_json IS NULL OR json_valid(stats_json)),
    started_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    finished_at    TEXT
);

CREATE TABLE import_issues (
    id            INTEGER PRIMARY KEY,
    import_job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    location      TEXT,
    severity      TEXT NOT NULL CHECK (severity IN ('HATA','UYARI','BILGI')),
    field         TEXT,
    message       TEXT NOT NULL,
    raw_value     TEXT,
    resolution    TEXT
);
CREATE INDEX ix_import_issues_job ON import_issues(import_job_id);

CREATE TABLE price_books (
    id             INTEGER PRIMARY KEY,
    authority_id   INTEGER NOT NULL REFERENCES authorities(id) ON DELETE RESTRICT,
    title          TEXT NOT NULL CHECK (length(trim(title)) > 0),
    year           INTEGER NOT NULL CHECK (year BETWEEN 1950 AND 2200),
    edition        TEXT NOT NULL DEFAULT '',
    valid_from     TEXT,
    status         TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK','AKTIF','ARSIV')),
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE RESTRICT,
    import_job_id  INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (authority_id, year, edition)
);

-- Poz kimliği: bir idarenin bir poz kodu. Yıllar arası fiyat geçmişini bağlar.
CREATE TABLE positions (
    id            INTEGER PRIMARY KEY,
    authority_id  INTEGER NOT NULL REFERENCES authorities(id) ON DELETE RESTRICT,
    code          TEXT NOT NULL,
    code_norm     TEXT NOT NULL CHECK (length(code_norm) > 0),
    category_path TEXT,
    UNIQUE (authority_id, code_norm)
);
CREATE INDEX ix_positions_code_norm ON positions(code_norm);

-- Bir pozun belirli bir kitaptaki kaydı: tarif, birim ve fiyat o kitaba aittir.
CREATE TABLE position_prices (
    id             INTEGER PRIMARY KEY,
    price_book_id  INTEGER NOT NULL REFERENCES price_books(id) ON DELETE RESTRICT,
    position_id    INTEGER NOT NULL REFERENCES positions(id) ON DELETE RESTRICT,
    description    TEXT NOT NULL,
    unit           TEXT NOT NULL,
    unit_norm      TEXT NOT NULL,
    unit_price     TEXT NOT NULL CHECK (unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*'),
    currency       TEXT NOT NULL DEFAULT 'TRY',
    search_text    TEXT NOT NULL,
    source_page    INTEGER,
    source_row     INTEGER,
    raw_text       TEXT,
    ocr_confidence REAL CHECK (ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 100),
    UNIQUE (price_book_id, position_id)
);
CREATE INDEX ix_position_prices_position ON position_prices(position_id);

CREATE VIRTUAL TABLE position_prices_fts USING fts5(
    search_text,
    content='position_prices',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER trg_position_prices_ai AFTER INSERT ON position_prices BEGIN
    INSERT INTO position_prices_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

CREATE TRIGGER trg_position_prices_ad AFTER DELETE ON position_prices BEGIN
    INSERT INTO position_prices_fts(position_prices_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
END;

CREATE TRIGGER trg_position_prices_au AFTER UPDATE OF search_text ON position_prices BEGIN
    INSERT INTO position_prices_fts(position_prices_fts, rowid, search_text)
    VALUES ('delete', old.id, old.search_text);
    INSERT INTO position_prices_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

CREATE TABLE projects (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    authority_id INTEGER REFERENCES authorities(id) ON DELETE RESTRICT,
    ikn          TEXT,
    tender_date  TEXT,
    work_type    TEXT,
    offer_type   TEXT NOT NULL DEFAULT 'BIRIM_FIYAT' CHECK (offer_type IN ('BIRIM_FIYAT','GOTURU_BEDEL')),
    status       TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK','DEVAM','TAMAMLANDI','ARSIV')),
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- Özel pozlar resmî pozlardan yapısal olarak ayrıdır; kod öneki "ÖZ-" zorunludur.
CREATE TABLE custom_positions (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE CHECK (substr(code, 1, 3) = 'ÖZ-' AND length(code) > 3),
    code_norm  TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    category   TEXT,
    status     TEXT NOT NULL DEFAULT 'AKTIF' CHECK (status IN ('AKTIF','PASIF')),
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE custom_position_versions (
    id                 INTEGER PRIMARY KEY,
    custom_position_id INTEGER NOT NULL REFERENCES custom_positions(id) ON DELETE RESTRICT,
    version_no         INTEGER NOT NULL CHECK (version_no >= 1),
    description        TEXT NOT NULL,
    unit               TEXT NOT NULL,
    unit_norm          TEXT NOT NULL,
    price_method       TEXT NOT NULL CHECK (price_method IN ('DOGRUDAN','ANALIZ')),
    direct_price       TEXT CHECK (direct_price IS NULL OR (direct_price GLOB '[0-9]*' AND direct_price NOT GLOB '*[^0-9.]*')),
    computed_price     TEXT CHECK (computed_price IS NULL OR (computed_price GLOB '[0-9]*' AND computed_price NOT GLOB '*[^0-9.]*')),
    price_date         TEXT NOT NULL,
    basis_text         TEXT NOT NULL DEFAULT '',
    locked             INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (custom_position_id, version_no),
    CHECK ((price_method = 'DOGRUDAN' AND direct_price IS NOT NULL)
        OR (price_method = 'ANALIZ' AND direct_price IS NULL))
);

CREATE TRIGGER trg_custom_version_locked_upd BEFORE UPDATE ON custom_position_versions
WHEN old.locked = 1 BEGIN
    SELECT RAISE(ABORT, 'Kilitli özel poz sürümü değiştirilemez; yeni sürüm oluşturun.');
END;

CREATE TRIGGER trg_custom_version_locked_del BEFORE DELETE ON custom_position_versions
WHEN old.locked = 1 BEGIN
    SELECT RAISE(ABORT, 'Kilitli özel poz sürümü silinemez.');
END;

CREATE TABLE analyses (
    id                         INTEGER PRIMARY KEY,
    custom_position_version_id INTEGER NOT NULL UNIQUE REFERENCES custom_position_versions(id) ON DELETE CASCADE,
    base_quantity              TEXT NOT NULL DEFAULT '1' CHECK (base_quantity GLOB '[0-9]*' AND base_quantity NOT GLOB '*[^0-9.]*'),
    overhead_rate              TEXT NOT NULL DEFAULT '0' CHECK (overhead_rate GLOB '[0-9]*' AND overhead_rate NOT GLOB '*[^0-9.]*'),
    profit_rate                TEXT NOT NULL DEFAULT '0' CHECK (profit_rate GLOB '[0-9]*' AND profit_rate NOT GLOB '*[^0-9.]*'),
    rule_source_text           TEXT NOT NULL DEFAULT '',
    notes                      TEXT
);

CREATE TABLE analysis_items (
    id                    INTEGER PRIMARY KEY,
    analysis_id           INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    line_no               INTEGER NOT NULL CHECK (line_no >= 1),
    item_type             TEXT NOT NULL CHECK (item_type IN ('MALZEME','ISCILIK','MAKINE','NAKLIYE','DIGER')),
    ref_position_price_id INTEGER REFERENCES position_prices(id) ON DELETE RESTRICT,
    description           TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    quantity              TEXT NOT NULL CHECK (quantity GLOB '[0-9]*' AND quantity NOT GLOB '*[^0-9.]*'),
    unit_price            TEXT NOT NULL CHECK (unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*'),
    waste_rate            TEXT NOT NULL DEFAULT '0' CHECK (waste_rate GLOB '[0-9]*' AND waste_rate NOT GLOB '*[^0-9.]*'),
    coefficient           TEXT NOT NULL DEFAULT '1' CHECK (coefficient GLOB '[0-9]*' AND coefficient NOT GLOB '*[^0-9.]*'),
    unit_conversion       TEXT NOT NULL DEFAULT '1' CHECK (unit_conversion GLOB '[0-9]*' AND unit_conversion NOT GLOB '*[^0-9.]*'),
    source_text           TEXT,
    UNIQUE (analysis_id, line_no)
);

CREATE TABLE estimates (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    name          TEXT NOT NULL CHECK (length(trim(name)) > 0),
    revision      INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    import_job_id INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL,
    locked        INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (project_id, name, revision)
);

CREATE TABLE estimate_items (
    id                  INTEGER PRIMARY KEY,
    estimate_id         INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    line_no             INTEGER NOT NULL CHECK (line_no >= 1),
    work_group          TEXT,
    raw_code            TEXT,
    raw_description     TEXT,
    raw_unit            TEXT,
    quantity            TEXT NOT NULL CHECK (quantity GLOB '[0-9]*' AND quantity NOT GLOB '*[^0-9.]*'),
    raw_unit_price      TEXT,
    raw_amount          TEXT,
    notes               TEXT,
    match_status        TEXT NOT NULL DEFAULT 'ESLESMEDI'
                        CHECK (match_status IN ('ESLESMEDI','ONERILDI','ONAYLANDI','ERTELENDI')),
    position_price_id   INTEGER REFERENCES position_prices(id) ON DELETE RESTRICT,
    custom_version_id   INTEGER REFERENCES custom_position_versions(id) ON DELETE RESTRICT,
    unit_mismatch_ack   INTEGER NOT NULL DEFAULT 0 CHECK (unit_mismatch_ack IN (0, 1)),
    user_price_override TEXT CHECK (user_price_override IS NULL OR (user_price_override GLOB '[0-9]*' AND user_price_override NOT GLOB '*[^0-9.]*')),
    matched_by          TEXT CHECK (matched_by IS NULL OR matched_by IN ('OTOMATIK','KULLANICI')),
    matched_at          TEXT,
    UNIQUE (estimate_id, line_no),
    CHECK (position_price_id IS NULL OR custom_version_id IS NULL),
    CHECK (match_status <> 'ONAYLANDI' OR position_price_id IS NOT NULL OR custom_version_id IS NOT NULL),
    CHECK (raw_code IS NOT NULL OR raw_description IS NOT NULL)
);
CREATE INDEX ix_estimate_items_status ON estimate_items(estimate_id, match_status);

-- Hesap profilleri: mevzuat kuralları ve firma tercihleri sürümlüdür, doğrulama bayrağı taşır.
CREATE TABLE calc_profiles (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('REFERANS','YAKLASIK','FIRMA','TEKLIF')),
    name             TEXT NOT NULL,
    version          INTEGER NOT NULL CHECK (version >= 1),
    params_json      TEXT NOT NULL CHECK (json_valid(params_json)),
    legal_basis_text TEXT NOT NULL DEFAULT '',
    verified         INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    valid_from       TEXT,
    imported_from    TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (name, version)
);

CREATE TABLE calculations (
    id                   INTEGER PRIMARY KEY,
    estimate_id          INTEGER NOT NULL REFERENCES estimates(id) ON DELETE RESTRICT,
    profile_id           INTEGER NOT NULL REFERENCES calc_profiles(id) ON DELETE RESTRICT,
    kind                 TEXT NOT NULL CHECK (kind IN ('REFERANS','YAKLASIK','FIRMA','TEKLIF')),
    name                 TEXT,
    params_override_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_override_json)),
    source_priority_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_priority_json)),
    status               TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK','KESIN')),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE firm_cost_lines (
    id               INTEGER PRIMARY KEY,
    calculation_id   INTEGER NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
    estimate_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    material         TEXT NOT NULL DEFAULT '0' CHECK (material GLOB '[0-9]*' AND material NOT GLOB '*[^0-9.]*'),
    labour           TEXT NOT NULL DEFAULT '0' CHECK (labour GLOB '[0-9]*' AND labour NOT GLOB '*[^0-9.]*'),
    machine          TEXT NOT NULL DEFAULT '0' CHECK (machine GLOB '[0-9]*' AND machine NOT GLOB '*[^0-9.]*'),
    transport        TEXT NOT NULL DEFAULT '0' CHECK (transport GLOB '[0-9]*' AND transport NOT GLOB '*[^0-9.]*'),
    subcontract      TEXT NOT NULL DEFAULT '0' CHECK (subcontract GLOB '[0-9]*' AND subcontract NOT GLOB '*[^0-9.]*'),
    other            TEXT NOT NULL DEFAULT '0' CHECK (other GLOB '[0-9]*' AND other NOT GLOB '*[^0-9.]*'),
    source_note      TEXT,
    UNIQUE (calculation_id, estimate_item_id)
);

CREATE TABLE offer_scenarios (
    id                   INTEGER PRIMARY KEY,
    calculation_id       INTEGER NOT NULL REFERENCES calculations(id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    profit_rate          TEXT NOT NULL CHECK (profit_rate GLOB '[0-9]*' AND profit_rate NOT GLOB '*[^0-9.]*'),
    risk_rate            TEXT NOT NULL DEFAULT '0' CHECK (risk_rate GLOB '[0-9]*' AND risk_rate NOT GLOB '*[^0-9.]*'),
    group_overrides_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(group_overrides_json)),
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (calculation_id, name)
);

-- Kesinleştirilmiş hesapların değişmez kopyası.
CREATE TABLE calculation_snapshots (
    id             INTEGER PRIMARY KEY,
    calculation_id INTEGER NOT NULL REFERENCES calculations(id) ON DELETE RESTRICT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    app_version    TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json   TEXT NOT NULL CHECK (json_valid(payload_json)),
    sha256         TEXT NOT NULL CHECK (length(sha256) = 64)
);
CREATE INDEX ix_snapshots_calc ON calculation_snapshots(calculation_id);

CREATE TRIGGER trg_snapshots_no_update BEFORE UPDATE ON calculation_snapshots BEGIN
    SELECT RAISE(ABORT, 'Kesinleştirilmiş hesap kaydı değiştirilemez.');
END;

CREATE TRIGGER trg_snapshots_no_delete BEFORE DELETE ON calculation_snapshots BEGIN
    SELECT RAISE(ABORT, 'Kesinleştirilmiş hesap kaydı silinemez.');
END;

CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY,
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now','localtime')),
    entity    TEXT NOT NULL,
    entity_id INTEGER,
    action    TEXT NOT NULL CHECK (action IN ('EKLE','GUNCELLE','SIL','TOPLU')),
    old_json  TEXT CHECK (old_json IS NULL OR json_valid(old_json)),
    new_json  TEXT CHECK (new_json IS NULL OR json_valid(new_json)),
    note      TEXT
);
CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);
