-- Ortak taraf (firma) kaydı: işveren, yüklenici, taşeron, istekli ve tedarikçi tek tabloda.
-- Belgelerdeki firma adı metinleri (o belgede yazıldığı biçimiyle) korunur; yanlarına taraf bağı eklenir.
-- Eşleştirme anahtarı (name_key) uygulamada Türkçe kurallarla üretilir; mevcut satırlar açılışta bağlanır.

CREATE TABLE parties (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    name_key   TEXT NOT NULL UNIQUE CHECK (length(name_key) > 0),
    short_name TEXT NOT NULL DEFAULT '',
    tax_office TEXT NOT NULL DEFAULT '',
    tax_no     TEXT NOT NULL DEFAULT '',
    address    TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    contact    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    is_sample  INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE party_roles (
    party_id INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('ISVEREN', 'YUKLENICI', 'TASERON', 'ISTEKLI', 'TEDARIKCI')),
    PRIMARY KEY (party_id, role)
);

-- Eski yazımlar ve birleştirilen kayıtların adları: aynı firma yeniden ayrı kayıt olarak açılmaz.
CREATE TABLE party_aliases (
    name_key TEXT PRIMARY KEY CHECK (length(name_key) > 0),
    party_id INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE
);

ALTER TABLE pp_contracts ADD COLUMN employer_party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL;
ALTER TABLE pp_contracts ADD COLUMN contractor_party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL;
ALTER TABLE tender_bids ADD COLUMN party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL;
ALTER TABLE purchase_offers ADD COLUMN party_id INTEGER REFERENCES parties(id) ON DELETE SET NULL;

CREATE INDEX ix_pp_contracts_employer_party ON pp_contracts(employer_party_id);
CREATE INDEX ix_pp_contracts_contractor_party ON pp_contracts(contractor_party_id);
CREATE INDEX ix_tender_bids_party ON tender_bids(party_id);
CREATE INDEX ix_purchase_offers_party ON purchase_offers(party_id);

-- Firma adı değişirse bağ yeniden kurulmak üzere boşaltılır (aynı güncellemede bağ da verildiyse dokunulmaz).
CREATE TRIGGER trg_pp_contracts_employer_renamed AFTER UPDATE OF employer ON pp_contracts
WHEN NEW.employer IS NOT OLD.employer AND NEW.employer_party_id IS OLD.employer_party_id
BEGIN
    UPDATE pp_contracts SET employer_party_id = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER trg_pp_contracts_contractor_renamed AFTER UPDATE OF contractor ON pp_contracts
WHEN NEW.contractor IS NOT OLD.contractor AND NEW.contractor_party_id IS OLD.contractor_party_id
BEGIN
    UPDATE pp_contracts SET contractor_party_id = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER trg_tender_bids_bidder_renamed AFTER UPDATE OF bidder ON tender_bids
WHEN NEW.bidder IS NOT OLD.bidder AND NEW.party_id IS OLD.party_id
BEGIN
    UPDATE tender_bids SET party_id = NULL WHERE id = NEW.id;
END;

CREATE TRIGGER trg_purchase_offers_firm_renamed AFTER UPDATE OF firm ON purchase_offers
WHEN NEW.firm IS NOT OLD.firm AND NEW.party_id IS OLD.party_id
BEGIN
    UPDATE purchase_offers SET party_id = NULL WHERE id = NEW.id;
END;
