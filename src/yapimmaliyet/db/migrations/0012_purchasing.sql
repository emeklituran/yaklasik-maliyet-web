-- 0012: Satın alma fiyat mukayesesi ve onay. Birim fiyat kütüphanesinden ve mukayeseli keşiften bağımsızdır.
-- Tutarlar kanonik ondalık TEXT. Teklif revizyonu yeni kayıt olarak eklenir; eski revizyon silinmez.

CREATE TABLE purchase_requests (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title             TEXT NOT NULL CHECK (length(trim(title)) > 0),
    scope             TEXT NOT NULL DEFAULT '',
    eval_date         TEXT,
    status            TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK', 'ONAYLANDI', 'IPTAL')),
    selected_offer_id INTEGER REFERENCES purchase_offers(id) ON DELETE SET NULL,
    decision_reason   TEXT,
    evaluator         TEXT,
    approver          TEXT,
    decided_at        TEXT,
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE purchase_items (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    line_no     INTEGER NOT NULL,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    spec        TEXT NOT NULL DEFAULT '',
    unit        TEXT NOT NULL CHECK (length(trim(unit)) > 0),
    quantity    TEXT NOT NULL CHECK (quantity GLOB '[0-9]*' AND quantity NOT GLOB '*[^0-9.]*'),
    UNIQUE (request_id, line_no)
);

CREATE TABLE purchase_offers (
    id             INTEGER PRIMARY KEY,
    request_id     INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    firm           TEXT NOT NULL CHECK (length(trim(firm)) > 0),
    contact        TEXT NOT NULL DEFAULT '',
    offer_no       TEXT NOT NULL DEFAULT '',
    offer_date     TEXT,
    valid_until    TEXT,
    vat_rate       TEXT NOT NULL DEFAULT '20' CHECK (vat_rate GLOB '[0-9]*' AND vat_rate NOT GLOB '*[^0-9.]*'),
    shipping       TEXT NOT NULL DEFAULT '0' CHECK (shipping GLOB '[0-9]*' AND shipping NOT GLOB '*[^0-9.]*'),
    installation   TEXT NOT NULL DEFAULT '0' CHECK (installation GLOB '[0-9]*' AND installation NOT GLOB '*[^0-9.]*'),
    other_costs    TEXT NOT NULL DEFAULT '0' CHECK (other_costs GLOB '[0-9]*' AND other_costs NOT GLOB '*[^0-9.]*'),
    delivery_days  INTEGER CHECK (delivery_days IS NULL OR delivery_days >= 0),
    delivery_terms TEXT NOT NULL DEFAULT '',
    payment_terms  TEXT NOT NULL DEFAULT '',
    warranty       TEXT NOT NULL DEFAULT '',
    technical_ok   INTEGER CHECK (technical_ok IS NULL OR technical_ok IN (0, 1)),
    technical_note TEXT NOT NULL DEFAULT '',
    file_path      TEXT NOT NULL DEFAULT '',
    revision       INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    supersedes_id  INTEGER REFERENCES purchase_offers(id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'GECERLI' CHECK (status IN ('GECERLI', 'REVIZE_EDILDI', 'GERI_CEKILDI')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE purchase_offer_lines (
    id            INTEGER PRIMARY KEY,
    offer_id      INTEGER NOT NULL REFERENCES purchase_offers(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES purchase_items(id) ON DELETE CASCADE,
    brand_model   TEXT NOT NULL DEFAULT '',
    quantity      TEXT NOT NULL CHECK (quantity GLOB '[0-9]*' AND quantity NOT GLOB '*[^0-9.]*'),
    unit          TEXT NOT NULL,
    list_price    TEXT NOT NULL CHECK (list_price GLOB '[0-9]*' AND list_price NOT GLOB '*[^0-9.]*'),
    discount_rate TEXT NOT NULL DEFAULT '0' CHECK (discount_rate GLOB '[0-9]*' AND discount_rate NOT GLOB '*[^0-9.]*'),
    scope_ok      INTEGER NOT NULL DEFAULT 1 CHECK (scope_ok IN (0, 1)),
    note          TEXT NOT NULL DEFAULT '',
    UNIQUE (offer_id, item_id)
);

CREATE TABLE purchase_criteria (
    id         INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    weight     TEXT NOT NULL CHECK (weight GLOB '[0-9]*' AND weight NOT GLOB '*[^0-9.]*')
);

CREATE TABLE purchase_scores (
    criterion_id INTEGER NOT NULL REFERENCES purchase_criteria(id) ON DELETE CASCADE,
    offer_id     INTEGER NOT NULL REFERENCES purchase_offers(id) ON DELETE CASCADE,
    score        TEXT NOT NULL CHECK (score GLOB '[0-9]*' AND score NOT GLOB '*[^0-9.]*'),
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (criterion_id, offer_id)
);
