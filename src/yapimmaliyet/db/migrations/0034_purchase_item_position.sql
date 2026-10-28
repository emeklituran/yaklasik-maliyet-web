-- Satın alma talebi kalemi bir poza bağlanabilir: birim fiyat kütüphanesinden seçilen poz (RESMI) ya da kütüphanede
-- olmadığı için açılan özel poz (OZEL). Poz kodu, kaynağın adı ve kimliği kalemde saklanır; kitap sonradan silinse bile
-- kalem anlamını korur. Mevcut kalemler pozsuz kalır.
ALTER TABLE purchase_items ADD COLUMN pos_kind TEXT CHECK (pos_kind IN ('RESMI', 'OZEL'));
ALTER TABLE purchase_items ADD COLUMN pos_ref_id INTEGER;
ALTER TABLE purchase_items ADD COLUMN pos_code TEXT NOT NULL DEFAULT '';
ALTER TABLE purchase_items ADD COLUMN pos_source TEXT NOT NULL DEFAULT '';
