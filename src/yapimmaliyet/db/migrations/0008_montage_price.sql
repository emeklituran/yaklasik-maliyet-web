-- 0008: ÇŞB montajlı (tesisat) fiyat listelerindeki "Montaj Bedeli".
-- Kanonik ondalık TEXT; kaynakta olmayan ya da önceden aktarılmış kayıtlarda NULL kalır (geriye dönük doldurma yok).

ALTER TABLE position_prices ADD COLUMN montage_price TEXT
    CHECK (montage_price IS NULL OR (montage_price GLOB '[0-9]*' AND montage_price NOT GLOB '*[^0-9.]*'));
