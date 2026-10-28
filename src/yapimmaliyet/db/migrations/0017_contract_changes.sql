-- 0017: Sözleşme değişiklik kayıtları (iş artışı, iş eksilişi, süre uzatımı, ek protokol). Ayrı bir sözleşme tablosu
-- açılmaz: değişiklikler mevcut pp_contracts satırına bağlanır. Asıl sözleşme bedeli ve bitiş tarihi değiştirilmez;
-- güncel bedel = sözleşme bedeli + Σ tutar farkı, güncel bitiş = bitiş tarihi + Σ gün farkı (serviste hesaplanır).
-- Tutar farkı işaretli kanonik ondalık TEXT'tir (iş eksilişinde negatif).

CREATE TABLE pp_contract_changes (
    id            INTEGER PRIMARY KEY,
    contract_id   INTEGER NOT NULL REFERENCES pp_contracts(id) ON DELETE CASCADE,
    change_no     INTEGER NOT NULL CHECK (change_no >= 1),
    kind          TEXT NOT NULL CHECK (kind IN ('IS_ARTISI', 'IS_EKSILISI', 'SURE_UZATIMI', 'EK_PROTOKOL', 'DIGER')),
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    approval_date TEXT,
    approval_ref  TEXT,
    amount_delta  TEXT,
    days_delta    INTEGER,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (kind <> 'SURE_UZATIMI' OR (days_delta > 0 AND amount_delta IS NULL)),
    CHECK (kind NOT IN ('IS_ARTISI', 'IS_EKSILISI') OR amount_delta IS NOT NULL),
    UNIQUE (contract_id, change_no)
);
CREATE INDEX ix_pp_contract_changes_contract ON pp_contract_changes(contract_id, change_no);
