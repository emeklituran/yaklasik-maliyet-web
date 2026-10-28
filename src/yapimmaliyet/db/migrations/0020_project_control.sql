-- 0020: Proje kontrolü — iş programı, kesin hesap, gerçekleşen maliyet / nakit akışı, satın alma siparişi ve teslim.
-- Tutarlar kanonik ondalık TEXT. Hakediş, sözleşme değişikliği ve satın alma kayıtları buraya kopyalanmaz; raporlar
-- onları kendi tablolarından okur (tek veri kaynağı).

-- İş programı: sözleşmenin aylık planlanan iş tutarları (KDV hariç, fiyat farkı hariç). Dönem 'YYYY-AA'.
CREATE TABLE ws_plan_lines (
    id          INTEGER PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES pp_contracts(id) ON DELETE CASCADE,
    period      TEXT NOT NULL CHECK (period GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    amount      TEXT NOT NULL,
    note        TEXT,
    UNIQUE (contract_id, period)
);

-- Kesin hesap: sözleşme başına tek kayıt. Kesinleşince içerik JSON + SHA-256 ile sabitlenir; değiştirilemez, silinemez.
CREATE TABLE fa_final_accounts (
    id              INTEGER PRIMARY KEY,
    contract_id     INTEGER NOT NULL UNIQUE REFERENCES pp_contracts(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK', 'KESIN')),
    completion_date TEXT,
    acceptance_ref  TEXT,
    notes           TEXT,
    approved_by     TEXT,
    approved_at     TEXT,
    snapshot_json   TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
    snapshot_sha    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK ((status = 'KESIN') = (snapshot_json IS NOT NULL))
);

CREATE TRIGGER trg_fa_frozen BEFORE UPDATE ON fa_final_accounts
WHEN OLD.status = 'KESIN'
BEGIN SELECT RAISE(ABORT, 'Kesinleşmiş kesin hesap değiştirilemez.'); END;
CREATE TRIGGER trg_fa_nodelete BEFORE DELETE ON fa_final_accounts
WHEN OLD.status = 'KESIN'
BEGIN SELECT RAISE(ABORT, 'Kesinleşmiş kesin hesap silinemez.'); END;

-- Gerçekleşen maliyet: elle girilen gider kayıtları (KDV hariç tutar + KDV oranı). Ödeme tarihi nakit akışında kullanılır.
CREATE TABLE ac_cost_entries (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    entry_date  TEXT NOT NULL,
    category    TEXT NOT NULL CHECK (category IN ('MALZEME', 'ISCILIK', 'MAKINE', 'TASERON', 'GENEL_GIDER', 'DIGER')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    party       TEXT,
    document_no TEXT,
    amount      TEXT NOT NULL,
    vat_rate    TEXT NOT NULL DEFAULT '0.2000',
    paid_date   TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_ac_cost_entries_project ON ac_cost_entries(project_id, entry_date);

-- Maliyet bütçesi: proje × gider türü (KDV hariç).
CREATE TABLE ac_budgets (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    category   TEXT NOT NULL CHECK (category IN ('MALZEME', 'ISCILIK', 'MAKINE', 'TASERON', 'GENEL_GIDER', 'DIGER')),
    amount     TEXT NOT NULL,
    PRIMARY KEY (project_id, category)
);

-- Satın alma siparişi: onaylanmış satın alma talebinin seçilen tekliflerinden. Satırların miktarı ve net birim fiyatı
-- sipariş anında sabitlenir. Teslim kaydı olan sipariş silinemez, iptal edilemez.
CREATE TABLE po_orders (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER NOT NULL UNIQUE REFERENCES purchase_requests(id) ON DELETE RESTRICT,
    offer_id    INTEGER NOT NULL REFERENCES purchase_offers(id) ON DELETE RESTRICT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    order_no    TEXT NOT NULL CHECK (length(trim(order_no)) > 0),
    order_date  TEXT NOT NULL,
    supplier    TEXT NOT NULL CHECK (length(trim(supplier)) > 0),
    due_date    TEXT,
    vat_rate    TEXT NOT NULL,
    extra_costs TEXT NOT NULL DEFAULT '0.00',
    status      TEXT NOT NULL DEFAULT 'ACIK' CHECK (status IN ('ACIK', 'IPTAL')),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE po_order_lines (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES po_orders(id) ON DELETE CASCADE,
    item_id     INTEGER REFERENCES purchase_items(id) ON DELETE SET NULL,
    line_no     INTEGER NOT NULL,
    description TEXT NOT NULL,
    unit        TEXT NOT NULL,
    quantity    TEXT NOT NULL,
    unit_price  TEXT NOT NULL,
    UNIQUE (order_id, line_no)
);

CREATE TABLE po_deliveries (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES po_orders(id) ON DELETE RESTRICT,
    delivery_date TEXT NOT NULL,
    waybill_no    TEXT,
    received_by   TEXT,
    paid_date     TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_po_deliveries_order ON po_deliveries(order_id);

CREATE TABLE po_delivery_lines (
    id            INTEGER PRIMARY KEY,
    delivery_id   INTEGER NOT NULL REFERENCES po_deliveries(id) ON DELETE CASCADE,
    order_line_id INTEGER NOT NULL REFERENCES po_order_lines(id) ON DELETE RESTRICT,
    quantity      TEXT NOT NULL,
    UNIQUE (delivery_id, order_line_id)
);
