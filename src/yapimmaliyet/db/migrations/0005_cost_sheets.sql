-- 0005: Firma maliyeti çalışmaları ve teklif senaryoları.
-- 0001'deki firm_cost_lines ve offer_scenarios tabloları kesin hesaba bağlıydı ve hiç kullanılmadı
-- (veri içermez); çalışma (taslak) düzeyinde yeniden tanımlanır.
DROP TABLE firm_cost_lines;
DROP TABLE offer_scenarios;

CREATE TABLE cost_sheets (
    id          INTEGER PRIMARY KEY,
    estimate_id INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (estimate_id, name)
);

CREATE TABLE cost_sheet_lines (
    id               INTEGER PRIMARY KEY,
    sheet_id         INTEGER NOT NULL REFERENCES cost_sheets(id) ON DELETE CASCADE,
    estimate_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    material         TEXT NOT NULL DEFAULT '0' CHECK (material GLOB '[0-9]*' AND material NOT GLOB '*[^0-9.]*'),
    labour           TEXT NOT NULL DEFAULT '0' CHECK (labour GLOB '[0-9]*' AND labour NOT GLOB '*[^0-9.]*'),
    machine          TEXT NOT NULL DEFAULT '0' CHECK (machine GLOB '[0-9]*' AND machine NOT GLOB '*[^0-9.]*'),
    transport        TEXT NOT NULL DEFAULT '0' CHECK (transport GLOB '[0-9]*' AND transport NOT GLOB '*[^0-9.]*'),
    subcontract      TEXT NOT NULL DEFAULT '0' CHECK (subcontract GLOB '[0-9]*' AND subcontract NOT GLOB '*[^0-9.]*'),
    other            TEXT NOT NULL DEFAULT '0' CHECK (other GLOB '[0-9]*' AND other NOT GLOB '*[^0-9.]*'),
    note             TEXT,
    UNIQUE (sheet_id, estimate_item_id)
);

CREATE TABLE cost_sheet_indirects (
    id       INTEGER PRIMARY KEY,
    sheet_id INTEGER NOT NULL REFERENCES cost_sheets(id) ON DELETE CASCADE,
    name     TEXT NOT NULL CHECK (length(trim(name)) > 0),
    method   TEXT NOT NULL CHECK (method IN ('SABIT','ORAN')),
    value    TEXT NOT NULL CHECK (value GLOB '[0-9]*' AND value NOT GLOB '*[^0-9.]*'),
    note     TEXT
);

CREATE TABLE offer_scenarios (
    id          INTEGER PRIMARY KEY,
    sheet_id    INTEGER NOT NULL REFERENCES cost_sheets(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    profit_rate TEXT NOT NULL,
    risk_rate   TEXT NOT NULL DEFAULT '0',
    notes       TEXT,
    UNIQUE (sheet_id, name)
);

INSERT INTO calc_profiles(kind, name, version, params_json, legal_basis_text, verified)
VALUES ('FIRMA', 'Firma maliyeti ve teklif senaryoları', 1,
        '{"rounding":{"quantity_places":3,"price_places":2,"amount_places":2,"analysis_places":6,"mode":"HALF_UP"}}',
        'Firmanın kendi maliyet ve kârlılık varsayımları. Mevzuat kuralı içermez.', 1);
