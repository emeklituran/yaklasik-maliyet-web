-- Proje kontrolü: faaliyet kodu/WBS, içe aktarma batch izi, baz plan sürümleri (kilitli anlık görüntü), ilerleme snapshot'ları,
-- manuel aylık plan, faaliyet–keşif kapsam tahsisi ve kaynak maliyet–faaliyet oranlı tahsisi.
-- Yalnız ekleme: mevcut tablolar ve veriler korunur. Mevcut ac_cost_entries.activity_id bağları %100 tahsise çevrilir (tutar değişmez).

ALTER TABLE ws_activities ADD COLUMN external_code TEXT;
ALTER TABLE ws_activities ADD COLUMN wbs_code TEXT;
ALTER TABLE ws_activities ADD COLUMN import_batch_id INTEGER;
ALTER TABLE ws_activities ADD COLUMN source_ref TEXT;
CREATE UNIQUE INDEX ux_ws_activities_code ON ws_activities(project_id, external_code) WHERE external_code IS NOT NULL;

CREATE TABLE schedule_import_batches (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_name  TEXT NOT NULL,
    checksum     TEXT NOT NULL,
    mapping_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(mapping_json)),
    mode         TEXT NOT NULL DEFAULT 'ATLA' CHECK (mode IN ('ATLA', 'GUNCELLE')),
    status       TEXT NOT NULL CHECK (status IN ('TAMAM', 'REDDEDILDI')),
    row_count    INTEGER NOT NULL DEFAULT 0,
    added_count  INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE sched_baselines (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    reason      TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    approved_at TEXT,
    approved_by TEXT,
    supersedes_id INTEGER REFERENCES sched_baselines(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (project_id, revision_no),
    CHECK ((locked = 1) = (approved_at IS NOT NULL))
);

CREATE TABLE sched_baseline_items (
    id           INTEGER PRIMARY KEY,
    baseline_id  INTEGER NOT NULL REFERENCES sched_baselines(id) ON DELETE CASCADE,
    activity_id  INTEGER REFERENCES ws_activities(id) ON DELETE SET NULL,
    external_code TEXT,
    wbs_code     TEXT,
    title        TEXT NOT NULL,
    discipline   TEXT,
    planned_start TEXT NOT NULL,
    planned_end  TEXT NOT NULL,
    planned_cost TEXT NOT NULL,
    monthly_json TEXT CHECK (monthly_json IS NULL OR json_valid(monthly_json))
);
CREATE INDEX ix_sched_baseline_items ON sched_baseline_items(baseline_id);

-- Kilitli baz plan ve kalemleri değiştirilemez (düzeltme yeni sürümle yapılır).
CREATE TRIGGER trg_baseline_locked_update BEFORE UPDATE ON sched_baselines WHEN OLD.locked = 1
BEGIN SELECT RAISE(ABORT, 'Kilitli baz plan değiştirilemez; yeni revizyon açın.'); END;
CREATE TRIGGER trg_baseline_item_insert BEFORE INSERT ON sched_baseline_items
WHEN (SELECT locked FROM sched_baselines WHERE id = NEW.baseline_id) = 1
BEGIN SELECT RAISE(ABORT, 'Kilitli baz plana kalem eklenemez.'); END;
CREATE TRIGGER trg_baseline_item_update BEFORE UPDATE ON sched_baseline_items
WHEN (SELECT locked FROM sched_baselines WHERE id = OLD.baseline_id) = 1 AND NEW.activity_id IS OLD.activity_id
BEGIN SELECT RAISE(ABORT, 'Kilitli baz plan kalemi değiştirilemez.'); END;
CREATE TRIGGER trg_baseline_item_delete BEFORE DELETE ON sched_baseline_items
WHEN (SELECT locked FROM sched_baselines WHERE id = OLD.baseline_id) = 1
BEGIN SELECT RAISE(ABORT, 'Kilitli baz plan kalemi silinemez.'); END;

CREATE TABLE activity_progress_snapshots (
    id           INTEGER PRIMARY KEY,
    activity_id  INTEGER NOT NULL REFERENCES ws_activities(id) ON DELETE CASCADE,
    as_of        TEXT NOT NULL,
    physical_pct TEXT NOT NULL CHECK (CAST(physical_pct AS REAL) BETWEEN 0 AND 100),
    method       TEXT NOT NULL DEFAULT 'MANUEL_YUZDE' CHECK (method IN ('MANUEL_YUZDE', 'MIKTAR')),
    note         TEXT NOT NULL DEFAULT '',
    approved_by  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (activity_id, as_of)
);

CREATE TABLE activity_monthly_plan (
    activity_id INTEGER NOT NULL REFERENCES ws_activities(id) ON DELETE CASCADE,
    period      TEXT NOT NULL CHECK (period GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    amount      TEXT NOT NULL,
    PRIMARY KEY (activity_id, period)
);

-- Faaliyet ↔ keşif kalemi (M:N): ratio = kalemin bu faaliyete ayrılan yüzdesi; her kalem için Σ ≤ 100 (serviste zorlanır).
CREATE TABLE activity_scope_allocations (
    id               INTEGER PRIMARY KEY,
    activity_id      INTEGER NOT NULL REFERENCES ws_activities(id) ON DELETE CASCADE,
    estimate_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    ratio            TEXT NOT NULL CHECK (CAST(ratio AS REAL) > 0 AND CAST(ratio AS REAL) <= 100),
    note             TEXT NOT NULL DEFAULT '',
    UNIQUE (activity_id, estimate_item_id)
);

-- Kaynak maliyet olayı ↔ faaliyet oranlı tahsisi. Tutar KAYNAKTAN hesaplanır (kopya tutulmaz); her kaynak için Σ ratio ≤ 100.
CREATE TABLE cost_activity_allocations (
    id          INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('GIDER', 'TESLIM', 'TASERON_HAKEDIS')),
    source_id   INTEGER NOT NULL,
    activity_id INTEGER NOT NULL REFERENCES ws_activities(id) ON DELETE CASCADE,
    ratio       TEXT NOT NULL CHECK (CAST(ratio AS REAL) > 0 AND CAST(ratio AS REAL) <= 100),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (source_type, source_id, activity_id)
);
CREATE INDEX ix_cost_alloc_source ON cost_activity_allocations(source_type, source_id);

INSERT INTO cost_activity_allocations(source_type, source_id, activity_id, ratio, note)
SELECT 'GIDER', id, activity_id, '100.00', 'Önceki faaliyet bağından aktarıldı' FROM ac_cost_entries WHERE activity_id IS NOT NULL;
