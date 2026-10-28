-- Faz 4.4: Özel poz analizinde alt analiz (başka bir özel pozu girdi olarak kullanma).
-- Girdi en fazla bir kaynağa bağlanabilir: kütüphane fiyatı (ref_position_price_id) YA DA özel poz
-- (ref_custom_position_id). Mevcut analizler etkilenmez (yeni sütun NULL).
-- Döngü ve derinlik denetimi uygulama katmanında (domain/sub_analysis.py) yapılır.

ALTER TABLE analysis_items ADD COLUMN ref_custom_position_id INTEGER
    REFERENCES custom_positions(id) ON DELETE RESTRICT
    CHECK (ref_custom_position_id IS NULL OR ref_position_price_id IS NULL);

CREATE INDEX ix_analysis_items_ref_custom ON analysis_items(ref_custom_position_id);
