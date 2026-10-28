-- 0002: Özel poz araması için normalize edilmiş arama metni.
ALTER TABLE custom_positions ADD COLUMN search_text TEXT NOT NULL DEFAULT '';
CREATE INDEX ix_custom_positions_status ON custom_positions(status);
CREATE INDEX ix_price_books_status ON price_books(status);
