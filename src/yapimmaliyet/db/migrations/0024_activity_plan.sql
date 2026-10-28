-- Faaliyet bazlı iş programı ve maliyet planı (belirli bir dosya biçimine bağlı değildir).
-- Faaliyet: projeye bağlı, isteğe bağlı olarak disiplin, alt yüklenici iş paketi, keşif kalemi ve sözleşmeyle ilişkili iş.
-- Planlanan maliyet başlangıç–bitiş arasına gün oranıyla dağıtılır (kural dokümante; gerçek çizelge gelince değiştirilebilir).
-- Gerçekleşen maliyet kayıtlardan okunur (kopyalanmaz): gider kayıtları, satın alma teslimleri, taşeron hakedişleri.

CREATE TABLE ws_activities (
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name             TEXT NOT NULL CHECK (length(trim(name)) > 0),
    discipline       TEXT CHECK (discipline IS NULL OR discipline IN ('INSAAT', 'ELEKTRIK', 'MEKANIK', 'ALTYAPI')),
    package_id       INTEGER REFERENCES sub_packages(id) ON DELETE SET NULL,
    estimate_item_id INTEGER REFERENCES estimate_items(id) ON DELETE SET NULL,
    contract_id      INTEGER REFERENCES pp_contracts(id) ON DELETE SET NULL,
    planned_start    TEXT NOT NULL,
    planned_end      TEXT NOT NULL,
    planned_qty      TEXT,
    unit             TEXT,
    planned_cost     TEXT NOT NULL,
    progress         TEXT NOT NULL DEFAULT '0.00' CHECK (CAST(progress AS REAL) BETWEEN 0 AND 100),
    progress_date    TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (planned_end >= planned_start)
);
CREATE INDEX ix_ws_activities_project ON ws_activities(project_id, planned_start);
