-- 0015: Hakediş (taşeron hakedişi ve idari hakediş). Ortak yapı: sözleşme → sözleşme kalemleri → hakedişler
-- (dönem miktarları, metraj satırları, kesintiler, durum geçmişi). Tutarlar kanonik ondalık TEXT.
-- Onaylanan hakedişin içeriği değiştirilemez (tetikleyiciler); düzeltme yeni revizyonla yapılır.
-- Kesinti oranları kod içine gömülmez; sözleşme bazında kullanıcı tanımlar (pp_deduction_types).

CREATE TABLE pp_contracts (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('TASERON', 'IDARI')),
    project_id      INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title           TEXT NOT NULL CHECK (length(trim(title)) > 0),
    employer        TEXT NOT NULL CHECK (length(trim(employer)) > 0),
    contractor      TEXT NOT NULL CHECK (length(trim(contractor)) > 0),
    contract_no     TEXT,
    contract_date   TEXT,
    ikn             TEXT,
    start_date      TEXT,
    end_date        TEXT,
    model           TEXT NOT NULL CHECK (model IN ('BIRIM_FIYAT', 'ANAHTAR_TESLIM', 'KARMA')),
    contract_amount TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'TL' CHECK (length(trim(currency)) > 0),
    vat_rate        TEXT NOT NULL DEFAULT '0.2000',
    vat_base        TEXT NOT NULL DEFAULT 'BRUT' CHECK (vat_base IN ('BRUT', 'NET')),
    advance_total   TEXT NOT NULL DEFAULT '0.00',
    signatures      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(signatures)),
    notes           TEXT,
    is_sample       INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_pp_contracts_kind ON pp_contracts(kind);

-- Birim fiyatlı kalem: sözleşme miktarı × birim fiyat. Götürü kalem: kalem bedeli × gerçekleşme oranı (0–1).
CREATE TABLE pp_items (
    id               INTEGER PRIMARY KEY,
    contract_id      INTEGER NOT NULL REFERENCES pp_contracts(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL CHECK (line_no >= 1),
    code             TEXT,
    description      TEXT NOT NULL CHECK (length(trim(description)) > 0),
    unit             TEXT,
    discipline       TEXT CHECK (discipline IS NULL OR discipline IN ('INSAAT', 'ELEKTRIK', 'MEKANIK', 'ALTYAPI')),
    group_name       TEXT,
    method           TEXT NOT NULL CHECK (method IN ('BIRIM_FIYAT', 'GOTURU')),
    contract_qty     TEXT,
    unit_price       TEXT,
    lump_amount      TEXT,
    source           TEXT NOT NULL DEFAULT 'SOZLESME'
                     CHECK (source IN ('SOZLESME', 'DEGISIKLIK', 'YENI_BIRIM_FIYAT', 'ILAVE')),
    source_ref       TEXT,
    estimate_item_id INTEGER REFERENCES estimate_items(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (method <> 'GOTURU' OR lump_amount IS NOT NULL),
    UNIQUE (contract_id, line_no)
);
CREATE INDEX ix_pp_items_contract ON pp_items(contract_id);

-- Onaylı hakedişte kullanılmış bir kalemin fiyat/miktar/bedel değişikliği gerekçesiyle burada izlenir.
CREATE TABLE pp_item_changes (
    id        INTEGER PRIMARY KEY,
    item_id   INTEGER NOT NULL REFERENCES pp_items(id) ON DELETE CASCADE,
    field     TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason    TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    ref       TEXT,
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- Kesinti türü: MANUEL (tutar girilir) ya da oranlı (BU_HAKEDIS, BU_HAKEDIS_EKSI_AVANS, KDV tabanı × oran).
CREATE TABLE pp_deduction_types (
    id           INTEGER PRIMARY KEY,
    contract_id  INTEGER NOT NULL REFERENCES pp_contracts(id) ON DELETE CASCADE,
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    base         TEXT NOT NULL CHECK (base IN ('MANUEL', 'BU_HAKEDIS', 'BU_HAKEDIS_EKSI_AVANS', 'KDV')),
    rate         TEXT,
    is_advance   INTEGER NOT NULL DEFAULT 0 CHECK (is_advance IN (0, 1)),
    is_guarantee INTEGER NOT NULL DEFAULT 0 CHECK (is_guarantee IN (0, 1)),
    sort_order   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    basis_note   TEXT,
    CHECK (base = 'MANUEL' OR rate IS NOT NULL),
    CHECK (is_advance = 0 OR base = 'MANUEL'),
    UNIQUE (contract_id, name)
);

CREATE TABLE pp_payments (
    id                INTEGER PRIMARY KEY,
    contract_id       INTEGER NOT NULL REFERENCES pp_contracts(id) ON DELETE CASCADE,
    no                INTEGER NOT NULL CHECK (no >= 1),
    revision          INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    supersedes_id     INTEGER REFERENCES pp_payments(id),
    period_start      TEXT,
    period_end        TEXT NOT NULL,
    payment_date      TEXT,
    status            TEXT NOT NULL DEFAULT 'TASLAK'
                      CHECK (status IN ('TASLAK', 'KONTROL', 'ONAY', 'ODENDI', 'DUZELTILDI')),
    price_diff_mode   TEXT NOT NULL DEFAULT 'YOK' CHECK (price_diff_mode IN ('YOK', 'TUTAR', 'KATSAYI')),
    price_diff_amount TEXT,
    price_diff_params TEXT CHECK (price_diff_params IS NULL OR json_valid(price_diff_params)),
    price_diff_note   TEXT,
    notes             TEXT,
    snapshot_json     TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
    snapshot_sha      TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK ((status IN ('TASLAK', 'KONTROL')) = (snapshot_json IS NULL)),
    UNIQUE (contract_id, no, revision)
);
CREATE INDEX ix_pp_payments_contract ON pp_payments(contract_id, no);

-- Bu dönem miktarı: birim fiyatlı kalemde miktar, götürü kalemde gerçekleşme oranı artışı (0–1).
-- Negatif değer düzeltme amaçlı girilebilir (uyarı verilir); kümülatif negatif olamaz (serviste denetlenir).
CREATE TABLE pp_payment_lines (
    id         INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL REFERENCES pp_payments(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES pp_items(id) ON DELETE RESTRICT,
    period_qty TEXT NOT NULL,
    note       TEXT,
    UNIQUE (payment_id, item_id)
);

-- Metraj / ataşman satırları; varsa satırın bu dönem miktarı bunların toplamıdır.
CREATE TABLE pp_measurements (
    id          INTEGER PRIMARY KEY,
    payment_id  INTEGER NOT NULL REFERENCES pp_payments(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES pp_items(id) ON DELETE RESTRICT,
    line_no     INTEGER NOT NULL,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    quantity    TEXT NOT NULL,
    ref         TEXT
);
CREATE INDEX ix_pp_measurements_payment ON pp_measurements(payment_id, item_id);

CREATE TABLE pp_payment_deductions (
    id          INTEGER PRIMARY KEY,
    payment_id  INTEGER NOT NULL REFERENCES pp_payments(id) ON DELETE CASCADE,
    type_id     INTEGER NOT NULL REFERENCES pp_deduction_types(id) ON DELETE RESTRICT,
    description TEXT,
    amount      TEXT NOT NULL
);

CREATE TABLE pp_status_log (
    id          INTEGER PRIMARY KEY,
    payment_id  INTEGER NOT NULL REFERENCES pp_payments(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    user_name   TEXT NOT NULL CHECK (length(trim(user_name)) > 0),
    note        TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime'))
);

-- Yalnızca TASLAK hakedişin satırları, metrajı ve kesintileri değiştirilebilir.
CREATE TRIGGER trg_pp_lines_ins BEFORE INSERT ON pp_payment_lines
WHEN (SELECT status FROM pp_payments WHERE id = NEW.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin satırları değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_lines_upd BEFORE UPDATE ON pp_payment_lines
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin satırları değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_lines_del BEFORE DELETE ON pp_payment_lines
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin satırları değiştirilebilir.'); END;

CREATE TRIGGER trg_pp_meas_ins BEFORE INSERT ON pp_measurements
WHEN (SELECT status FROM pp_payments WHERE id = NEW.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin metrajı değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_meas_upd BEFORE UPDATE ON pp_measurements
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin metrajı değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_meas_del BEFORE DELETE ON pp_measurements
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin metrajı değiştirilebilir.'); END;

CREATE TRIGGER trg_pp_ded_ins BEFORE INSERT ON pp_payment_deductions
WHEN (SELECT status FROM pp_payments WHERE id = NEW.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin kesintileri değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_ded_upd BEFORE UPDATE ON pp_payment_deductions
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin kesintileri değiştirilebilir.'); END;
CREATE TRIGGER trg_pp_ded_del BEFORE DELETE ON pp_payment_deductions
WHEN (SELECT status FROM pp_payments WHERE id = OLD.payment_id) <> 'TASLAK'
BEGIN SELECT RAISE(ABORT, 'Yalnızca taslak hakedişin kesintileri değiştirilebilir.'); END;

-- Onaylanmış hakediş: içerik sabit; yalnızca ONAY → ODENDI ve ONAY/ODENDI → DUZELTILDI durum geçişi yapılabilir.
CREATE TRIGGER trg_pp_payments_frozen BEFORE UPDATE ON pp_payments
WHEN OLD.status IN ('ONAY', 'ODENDI', 'DUZELTILDI') AND (
    NEW.snapshot_json IS NOT OLD.snapshot_json OR NEW.snapshot_sha IS NOT OLD.snapshot_sha
    OR NEW.no IS NOT OLD.no OR NEW.revision IS NOT OLD.revision OR NEW.contract_id IS NOT OLD.contract_id
    OR NEW.period_start IS NOT OLD.period_start OR NEW.period_end IS NOT OLD.period_end
    OR NEW.payment_date IS NOT OLD.payment_date OR NEW.price_diff_mode IS NOT OLD.price_diff_mode
    OR NEW.price_diff_amount IS NOT OLD.price_diff_amount OR NEW.price_diff_params IS NOT OLD.price_diff_params
    OR NOT (NEW.status = OLD.status
            OR (OLD.status = 'ONAY' AND NEW.status IN ('ODENDI', 'DUZELTILDI'))
            OR (OLD.status = 'ODENDI' AND NEW.status = 'DUZELTILDI')))
BEGIN SELECT RAISE(ABORT, 'Onaylanmış hakediş değiştirilemez; düzeltme için yeni revizyon oluşturun.'); END;

CREATE TRIGGER trg_pp_payments_nodelete BEFORE DELETE ON pp_payments
WHEN OLD.status IN ('ONAY', 'ODENDI', 'DUZELTILDI')
BEGIN SELECT RAISE(ABORT, 'Onaylanmış hakediş silinemez.'); END;
