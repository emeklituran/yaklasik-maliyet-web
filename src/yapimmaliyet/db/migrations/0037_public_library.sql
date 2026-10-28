-- 0037: Kamu birim fiyat kütüphanesi: kurum kataloğu, fiyat dönemi ve geçerlilik, fiyat türü, doğrulama durumu,
-- alt poz (/A, /B, /D) kuralları ve kararları, keşif fiyat güncellemesi revizyonları, doğrulanmamış poz kontrol listesi.
-- Var olan kitap/poz/analiz tabloları genişletilir; ikinci bir kütüphane kurulmaz. Yayın, geçerlilik ve içe aktarma
-- tarihleri ayrı alanlardır; kaynakta olmayan tarih boş (Belirtilmemiş) kalır. Geri dönüş: migration öncesi otomatik yedek.

-- Kurum kataloğu (resmî kaynak tanımı). Kod kurumlar arası benzersizdir; poz kodu kurumla birlikte benzersizdir.
ALTER TABLE authorities ADD COLUMN code TEXT;
ALTER TABLE authorities ADD COLUMN url TEXT;
ALTER TABLE authorities ADD COLUMN scope TEXT;
ALTER TABLE authorities ADD COLUMN catalog_period TEXT;   -- katalogda duyurulan son dönem ("2026-01", "2026")
ALTER TABLE authorities ADD COLUMN catalog_note TEXT;
CREATE UNIQUE INDEX ux_authorities_code ON authorities(code) WHERE code IS NOT NULL;

-- Kitabın fiyat dönemi ve geçerliliği. valid_from (0001) geçerlilik başlangıcıdır; created_at içe aktarma tarihidir.
ALTER TABLE price_books ADD COLUMN period TEXT;           -- "2026 Ocak", "2026/2", "2026 1. Dönem"
ALTER TABLE price_books ADD COLUMN valid_to TEXT
    CHECK (valid_to IS NULL OR valid_to GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]');
ALTER TABLE price_books ADD COLUMN published_on TEXT
    CHECK (published_on IS NULL OR published_on GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]');
ALTER TABLE price_books ADD COLUMN revision_no TEXT;
ALTER TABLE price_books ADD COLUMN revised_on TEXT
    CHECK (revised_on IS NULL OR revised_on GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]');
ALTER TABLE price_books ADD COLUMN price_kind TEXT NOT NULL DEFAULT 'BELIRTILMEMIS'
    CHECK (price_kind IN ('MALZEME','MONTAJ','MALZEME_MONTAJ','DEMONTAJ','DEMONTAJDAN_MONTAJ','RAYIC','ANALIZ','DIGER',
                          'BELIRTILMEMIS'));
-- KULLANICI: kullanıcının kendi aktardığı kitap (aktifleştirmek kullanıcının onayıdır). ADAY / KONTROL kitap aktif olamaz.
ALTER TABLE price_books ADD COLUMN verification TEXT NOT NULL DEFAULT 'KULLANICI'
    CHECK (verification IN ('ADAY','KONTROL','DOGRULANDI','KULLANICI'));
ALTER TABLE price_books ADD COLUMN source_ref TEXT;      -- kaynak belge adı (cilt, sayfa aralığı)
ALTER TABLE position_prices ADD COLUMN printed_page TEXT; -- belgede basılı sayfa numarası (PDF sayfası: source_page)

ALTER TABLE analysis_books ADD COLUMN period TEXT;
ALTER TABLE analysis_books ADD COLUMN valid_from TEXT
    CHECK (valid_from IS NULL OR valid_from GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]');

-- Keşif kaleminin fiyat kopyasına kurum ve dönem (raporlarda fiyat kaynağı; kitap silinse de kalır).
ALTER TABLE estimate_items ADD COLUMN snap_authority TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_period TEXT;
UPDATE estimate_items SET
    snap_authority = (SELECT COALESCE(a.short_name, a.name) FROM price_books pb JOIN authorities a ON a.id = pb.authority_id
                      WHERE pb.id = estimate_items.snap_book_id)
WHERE snap_book_id IS NOT NULL;

-- Alt poz kuralları: programın kullanıcı tarafından değiştirilebilir varsayılan yorumu; resmî tarif değildir.
-- En özel kural geçerlidir: proje > kurum > genel. Kapalı kural öneri üretmez.
CREATE TABLE subcode_rules (
    id           INTEGER PRIMARY KEY,
    suffix       TEXT NOT NULL CHECK (length(suffix) BETWEEN 2 AND 6 AND substr(suffix, 1, 1) IN ('/', '-')),
    meaning      TEXT NOT NULL CHECK (length(trim(meaning)) > 0),
    kind         TEXT NOT NULL CHECK (kind IN ('VIDALI','KAYNAKLI','DEMONTAJ','DIGER')),
    price_policy TEXT NOT NULL CHECK (price_policy IN ('ANA_FIYAT_ONERILIR','AYRI_FIYAT_GEREKIR')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    authority_id INTEGER REFERENCES authorities(id) ON DELETE CASCADE,
    project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    is_default   INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    notes        TEXT,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
CREATE UNIQUE INDEX ux_subcode_rules_scope ON subcode_rules(suffix, IFNULL(authority_id, 0), IFNULL(project_id, 0));
INSERT INTO subcode_rules(suffix, meaning, kind, price_policy, is_default, notes) VALUES
    ('/A', 'Vidalı bağlantı', 'VIDALI', 'ANA_FIYAT_ONERILIR', 1, 'Program varsayılanı; kurumun resmî tarifi değildir.'),
    ('/B', 'Kaynaklı bağlantı', 'KAYNAKLI', 'ANA_FIYAT_ONERILIR', 1, 'Program varsayılanı; kurumun resmî tarifi değildir.'),
    ('/D', 'Demontaj', 'DEMONTAJ', 'AYRI_FIYAT_GEREKIR', 1, 'Demontaj bedeli montaj fiyatından türetilmez.'),
    ('-D', 'Demontaj', 'DEMONTAJ', 'AYRI_FIYAT_GEREKIR', 1, 'Demontaj bedeli montaj fiyatından türetilmez.');

-- Alt poz önerisine verilen kullanıcı kararı (onay, düzenleyerek onay, ret) ve o anki kaynak bilgisi.
CREATE TABLE subcode_decisions (
    id                INTEGER PRIMARY KEY,
    estimate_item_id  INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    original_code     TEXT NOT NULL,
    base_code         TEXT NOT NULL,
    suffix            TEXT NOT NULL,
    rule_meaning      TEXT,
    decision          TEXT NOT NULL CHECK (decision IN ('ONAY','DUZENLENEREK_ONAY','RED')),
    position_price_id INTEGER REFERENCES position_prices(id) ON DELETE SET NULL,
    authority_name    TEXT,
    book_label        TEXT,
    period            TEXT,
    valid_from        TEXT,
    source_price      TEXT,
    applied_price     TEXT,
    note              TEXT,
    user_name         TEXT,
    decided_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
CREATE INDEX ix_subcode_decisions_item ON subcode_decisions(estimate_item_id);

-- "Birim Fiyatları Güncelle" öncesi kalemlerin fiyat kopyası: geri alma kaydı.
CREATE TABLE price_revisions (
    id          INTEGER PRIMARY KEY,
    estimate_id INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    note        TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0,
    undone_at   TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
CREATE TABLE price_revision_items (
    revision_id      INTEGER NOT NULL REFERENCES price_revisions(id) ON DELETE CASCADE,
    estimate_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    snapshot_json    TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    PRIMARY KEY (revision_id, estimate_item_id)
);

-- Doğrulanmamış poz kodu adayları (kontrol listesi). Fiyat kütüphanesine yazılmaz, hesaplarda kullanılmaz.
CREATE TABLE code_candidates (
    id            INTEGER PRIMARY KEY,
    authority_id  INTEGER NOT NULL REFERENCES authorities(id) ON DELETE CASCADE,
    code          TEXT NOT NULL CHECK (length(trim(code)) > 0),
    code_norm     TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('KITAP_KOD','ANALIZ_POZ','FIYAT')),
    preview       TEXT,
    unit          TEXT,
    price         TEXT CHECK (price IS NULL OR (price GLOB '[0-9]*' AND price NOT GLOB '*[^0-9.]*')),
    period        TEXT,
    document_name TEXT NOT NULL,
    document_sha  TEXT NOT NULL,
    page          INTEGER,
    status        TEXT NOT NULL DEFAULT 'ADAY' CHECK (status IN ('ADAY','INCELENDI','REDDEDILDI')),
    imported_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    UNIQUE (authority_id, code_norm, kind, document_sha, page)
);
CREATE INDEX ix_code_candidates_code ON code_candidates(code_norm);
