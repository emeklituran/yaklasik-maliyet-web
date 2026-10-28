-- Keşif kalemi, kütüphaneden çekilen poz bilgisini (kod, tanım, birim, birim fiyat, kaynak) kendi içinde saklar: kütüphanedeki
-- fiyat değişse ya da kitap silinse keşif etkilenmez. Güncelleme kullanıcının "Birim Fiyatları Güncelle" işlemiyle yapılır.
-- Kitap silinirken keşif/analizin kullandığı poz kayıtları, kullanıcıya görünmeyen (hidden) bir saklama kitabına taşınır.
ALTER TABLE price_books ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1));
ALTER TABLE estimate_items ADD COLUMN snap_code TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_description TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_unit TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_price TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_source TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_valid TEXT;
ALTER TABLE estimate_items ADD COLUMN snap_year INTEGER;
ALTER TABLE estimate_items ADD COLUMN snap_book_id INTEGER;
UPDATE estimate_items SET
    snap_code = (SELECT p.code FROM position_prices pp JOIN positions p ON p.id = pp.position_id WHERE pp.id = estimate_items.position_price_id),
    snap_description = (SELECT pp.description FROM position_prices pp WHERE pp.id = estimate_items.position_price_id),
    snap_unit = (SELECT pp.unit FROM position_prices pp WHERE pp.id = estimate_items.position_price_id),
    snap_price = (SELECT pp.unit_price FROM position_prices pp WHERE pp.id = estimate_items.position_price_id),
    snap_source = (SELECT COALESCE(a.short_name, a.name) || ' — ' || pb.title || ' ' || pb.year ||
                          CASE WHEN pb.edition IS NOT NULL AND pb.edition <> '' THEN ' ' || pb.edition ELSE '' END
                   FROM position_prices pp JOIN price_books pb ON pb.id = pp.price_book_id
                   JOIN authorities a ON a.id = pb.authority_id WHERE pp.id = estimate_items.position_price_id),
    snap_year = (SELECT pb.year FROM position_prices pp JOIN price_books pb ON pb.id = pp.price_book_id WHERE pp.id = estimate_items.position_price_id),
    snap_valid = (SELECT pb.valid_from FROM position_prices pp
                  JOIN price_books pb ON pb.id = pp.price_book_id WHERE pp.id = estimate_items.position_price_id),
    snap_book_id = (SELECT pp.price_book_id FROM position_prices pp WHERE pp.id = estimate_items.position_price_id)
WHERE position_price_id IS NOT NULL;
