-- Stok Faz 1 (docs/STOK_GECIS_TASARIMI.md): depo/saha kartları ve miktar hareket defteri. YALNIZ MİKTAR: hiçbir kayıt maliyet,
-- taahhüt ya da EAC üretmez; "teslim = maliyet" politikası değişmez. Hareketler eklenir, değiştirilmez/silinmez (düzeltme: ters hareket
-- ya da gerekçeli sayım düzeltmesi). Mevcut stk_issues korunur; isteğe bağlı depo bağlantısı için location_id eklenir.
CREATE TABLE stk_locations (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    note       TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE UNIQUE INDEX ux_stk_locations_name ON stk_locations(name COLLATE NOCASE);

ALTER TABLE stk_issues ADD COLUMN location_id INTEGER REFERENCES stk_locations(id) ON DELETE RESTRICT;

-- kind: IADE_DEPO (çıkışın depoya iadesi; to_location_id = dönülen yer, boş = varsayılan depo),
--       TRANSFER (from → to; boş uç = varsayılan depo), SAYIM (sayım düzeltmesi; miktar işaretli, to_location_id = yer).
CREATE TABLE stk_moves (
    id               INTEGER PRIMARY KEY,
    order_line_id    INTEGER NOT NULL REFERENCES po_order_lines(id) ON DELETE RESTRICT,
    kind             TEXT NOT NULL CHECK (kind IN ('IADE_DEPO', 'TRANSFER', 'SAYIM')),
    move_date        TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    from_location_id INTEGER REFERENCES stk_locations(id) ON DELETE RESTRICT,
    to_location_id   INTEGER REFERENCES stk_locations(id) ON DELETE RESTRICT,
    doc_no           TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (kind = 'SAYIM' OR CAST(quantity AS REAL) > 0),
    CHECK (kind <> 'SAYIM' OR (CAST(quantity AS REAL) <> 0 AND length(trim(reason)) > 0)),
    CHECK (kind = 'TRANSFER' OR from_location_id IS NULL),
    CHECK (kind <> 'TRANSFER' OR (from_location_id IS NOT NULL OR to_location_id IS NOT NULL) AND from_location_id IS NOT to_location_id)
);
CREATE INDEX ix_stk_moves_line ON stk_moves(order_line_id, move_date);

CREATE TRIGGER trg_stk_moves_no_update BEFORE UPDATE ON stk_moves
BEGIN SELECT RAISE(ABORT, 'Stok hareketi değiştirilemez; ters hareket ya da sayım düzeltmesi ekleyin.'); END;
CREATE TRIGGER trg_stk_moves_no_delete BEFORE DELETE ON stk_moves
BEGIN SELECT RAISE(ABORT, 'Stok hareketi silinemez; ters hareket ya da sayım düzeltmesi ekleyin.'); END;
