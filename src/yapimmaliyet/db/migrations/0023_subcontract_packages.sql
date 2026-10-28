-- Alt yüklenici iş paketleri ve teklif karşılaştırması.
-- İş paketi: projenin keşif kalemlerinden (ya da elle) oluşturulan, disiplin ve kapsamı belli iş grubu.
-- Teklifler tekil firma kimliğine (parties) bağlıdır; revizyonlar eskisini geçersiz kılar, silmez.
-- Seçilen teklif alt yüklenici (TASERON) sözleşmesine bağlanır; bağ pp_contracts.sub_bid_id ile tutulur.

CREATE TABLE sub_packages (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    estimate_id  INTEGER REFERENCES estimates(id) ON DELETE SET NULL,
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    discipline   TEXT CHECK (discipline IS NULL OR discipline IN ('INSAAT', 'ELEKTRIK', 'MEKANIK', 'ALTYAPI')),
    scope        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'TASLAK'
                 CHECK (status IN ('TASLAK', 'TEKLIF_ISTENDI', 'DEGERLENDIRMEDE', 'SECILDI', 'IPTAL')),
    selected_bid_id INTEGER,
    decision_note TEXT,
    decided_by   TEXT,
    decided_at   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_sub_packages_project ON sub_packages(project_id);

CREATE TABLE sub_package_items (
    id               INTEGER PRIMARY KEY,
    package_id       INTEGER NOT NULL REFERENCES sub_packages(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    estimate_item_id INTEGER REFERENCES estimate_items(id) ON DELETE SET NULL,
    code             TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL CHECK (length(trim(description)) > 0),
    unit             TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    UNIQUE (package_id, line_no)
);

CREATE TABLE sub_invites (
    package_id INTEGER NOT NULL REFERENCES sub_packages(id) ON DELETE CASCADE,
    party_id   INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    invited_on TEXT,
    PRIMARY KEY (package_id, party_id)
);

CREATE TABLE sub_bids (
    id            INTEGER PRIMARY KEY,
    package_id    INTEGER NOT NULL REFERENCES sub_packages(id) ON DELETE CASCADE,
    party_id      INTEGER NOT NULL REFERENCES parties(id) ON DELETE RESTRICT,
    revision      INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER REFERENCES sub_bids(id) ON DELETE SET NULL,
    status        TEXT NOT NULL DEFAULT 'GECERLI' CHECK (status IN ('GECERLI', 'REVIZE_EDILDI', 'GERI_CEKILDI')),
    bid_no        TEXT NOT NULL DEFAULT '',
    bid_date      TEXT,
    valid_until   TEXT,
    delivery_days INTEGER,
    payment_terms TEXT NOT NULL DEFAULT '',
    vat_included  INTEGER NOT NULL DEFAULT 0 CHECK (vat_included IN (0, 1)),
    vat_rate      TEXT NOT NULL DEFAULT '20.00',
    inclusions    TEXT NOT NULL DEFAULT '',
    exclusions    TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    file_path     TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_sub_bids_package ON sub_bids(package_id, party_id);
CREATE INDEX ix_sub_bids_party ON sub_bids(party_id);

-- Satır birim fiyatı KDV hariç ya da dahil olabilir (sub_bids.vat_included); kapsam dışı kalem işaretlenebilir.
CREATE TABLE sub_bid_lines (
    id       INTEGER PRIMARY KEY,
    bid_id   INTEGER NOT NULL REFERENCES sub_bids(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES sub_package_items(id) ON DELETE CASCADE,
    unit_price TEXT NOT NULL,
    in_scope INTEGER NOT NULL DEFAULT 1 CHECK (in_scope IN (0, 1)),
    note     TEXT NOT NULL DEFAULT '',
    UNIQUE (bid_id, item_id)
);

ALTER TABLE pp_contracts ADD COLUMN sub_bid_id INTEGER REFERENCES sub_bids(id) ON DELETE SET NULL;
