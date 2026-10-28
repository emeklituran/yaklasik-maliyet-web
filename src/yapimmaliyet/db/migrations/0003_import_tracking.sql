-- 0003: İçe aktarma izlenebilirliği ve sütun eşleme şablonları.
CREATE TABLE import_templates (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('FIYAT_KITABI','KESIF')),
    authority_id INTEGER REFERENCES authorities(id) ON DELETE CASCADE,
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    mapping_json TEXT NOT NULL CHECK (json_valid(mapping_json)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
CREATE UNIQUE INDEX ux_import_templates ON import_templates(kind, IFNULL(authority_id, 0), name);

ALTER TABLE import_jobs ADD COLUMN target_book_id INTEGER REFERENCES price_books(id) ON DELETE SET NULL;
ALTER TABLE import_jobs ADD COLUMN target_estimate_id INTEGER REFERENCES estimates(id) ON DELETE SET NULL;
ALTER TABLE position_prices ADD COLUMN import_job_id INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL;
CREATE INDEX ix_position_prices_import ON position_prices(import_job_id);
