-- Alt yüklenici performansı: kullanıcının girdiği değerlendirme notları (1–5). Notlar eklenir, değiştirilmez ya da silinmez;
-- göstergeler (fiyat, termin, hakediş düzeltmesi, tamamlanan iş) kayıttan hesaplanır ve burada saklanmaz.
CREATE TABLE sub_perf_notes (
    id          INTEGER PRIMARY KEY,
    party_id    INTEGER NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    contract_id INTEGER REFERENCES pp_contracts(id) ON DELETE SET NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    note        TEXT NOT NULL CHECK (length(trim(note)) > 0),
    rated_by    TEXT NOT NULL CHECK (length(trim(rated_by)) > 0),
    rated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_sub_perf_notes_party ON sub_perf_notes(party_id, id);

CREATE TRIGGER trg_sub_perf_notes_no_update BEFORE UPDATE ON sub_perf_notes
BEGIN SELECT RAISE(ABORT, 'Performans notu değiştirilemez; yeni not ekleyin.'); END;
CREATE TRIGGER trg_sub_perf_notes_no_delete BEFORE DELETE ON sub_perf_notes
WHEN EXISTS (SELECT 1 FROM parties WHERE id = OLD.party_id)
BEGIN SELECT RAISE(ABORT, 'Performans notu silinemez.'); END;
