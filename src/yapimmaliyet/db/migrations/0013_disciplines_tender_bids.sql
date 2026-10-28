-- 0013: Disiplinler (inşaat, elektrik, mekanik tesisat, altyapı), ihale modeli ve ihale teklifleri.
-- projects.offer_type (BIRIM_FIYAT/GOTURU_BEDEL) geriye uyumluluk için korunur ve tender_model ile eşlenir:
-- ANAHTAR_TESLIM → GOTURU_BEDEL, BIRIM_FIYAT ve KARMA → BIRIM_FIYAT. Asıl model tender_model sütunudur.

ALTER TABLE projects ADD COLUMN tender_model TEXT NOT NULL DEFAULT 'BIRIM_FIYAT'
    CHECK (tender_model IN ('BIRIM_FIYAT', 'ANAHTAR_TESLIM', 'KARMA'));
UPDATE projects SET tender_model = 'ANAHTAR_TESLIM' WHERE offer_type = 'GOTURU_BEDEL';

-- Projede yer alan disiplinler: virgülle ayrılmış kodlar (ör. "INSAAT,MEKANIK"); boş = belirtilmemiş.
ALTER TABLE projects ADD COLUMN disciplines TEXT NOT NULL DEFAULT '';

ALTER TABLE price_books ADD COLUMN discipline TEXT
    CHECK (discipline IS NULL OR discipline IN ('INSAAT', 'ELEKTRIK', 'MEKANIK', 'ALTYAPI'));

ALTER TABLE estimate_items ADD COLUMN discipline TEXT
    CHECK (discipline IS NULL OR discipline IN ('INSAAT', 'ELEKTRIK', 'MEKANIK', 'ALTYAPI'));
-- Karma modelde kalemin teklif yöntemi; diğer modellerde modelden türetilir ve boş kalabilir.
ALTER TABLE estimate_items ADD COLUMN pricing_method TEXT
    CHECK (pricing_method IS NULL OR pricing_method IN ('BIRIM_FIYAT', 'GOTURU'));

-- İhale teklifleri: bir keşif (yaklaşık maliyet) için istekli teklifleri. Tutarlar kanonik ondalık TEXT.
CREATE TABLE tender_bids (
    id          INTEGER PRIMARY KEY,
    estimate_id INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    bidder      TEXT NOT NULL CHECK (length(trim(bidder)) > 0),
    bid_date    TEXT,
    lump_total  TEXT CHECK (lump_total IS NULL OR (lump_total GLOB '[0-9]*' AND lump_total NOT GLOB '*[^0-9.]*')),
    is_sample   INTEGER NOT NULL DEFAULT 0 CHECK (is_sample IN (0, 1)),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (estimate_id, bidder)
);

-- Birim fiyatlı kalemde teklif birim fiyatı, götürü bedelli kalemde (karma model) kalem götürü tutarı girilir.
CREATE TABLE tender_bid_lines (
    id               INTEGER PRIMARY KEY,
    bid_id           INTEGER NOT NULL REFERENCES tender_bids(id) ON DELETE CASCADE,
    estimate_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    unit_price       TEXT CHECK (unit_price IS NULL OR (unit_price GLOB '[0-9]*' AND unit_price NOT GLOB '*[^0-9.]*')),
    lump_sum         TEXT CHECK (lump_sum IS NULL OR (lump_sum GLOB '[0-9]*' AND lump_sum NOT GLOB '*[^0-9.]*')),
    CHECK ((unit_price IS NULL) <> (lump_sum IS NULL)),
    UNIQUE (bid_id, estimate_item_id)
);
CREATE INDEX ix_tender_bid_lines_bid ON tender_bid_lines(bid_id);
