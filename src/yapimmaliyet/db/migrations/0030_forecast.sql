-- Taahhütsüz kalan iş tahmini (ETC) sürümleri ve tahmin anlık görüntüleri (EAC, gelir, beklenen kâr).
-- Tahminler kullanıcı onaylı varsayımdır: sürümlü, gerekçeli, tarihli; geçmiş sürümler silinmez.
CREATE TABLE forecast_etc (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scenario    TEXT NOT NULL CHECK (scenario IN ('IYIMSER', 'BAZ', 'KOTUMSER')),
    version     INTEGER NOT NULL CHECK (version >= 1),
    amount      TEXT NOT NULL CHECK (CAST(amount AS REAL) >= 0),
    reason      TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    as_of       TEXT NOT NULL,
    confirmed_by TEXT NOT NULL CHECK (length(trim(confirmed_by)) > 0),
    excludes_open_commitment INTEGER NOT NULL CHECK (excludes_open_commitment = 1),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (project_id, scenario, version)
);

CREATE TABLE forecast_snapshots (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    as_of           TEXT NOT NULL,
    scenario        TEXT NOT NULL,
    bac             TEXT,
    ac              TEXT NOT NULL,
    open_commitment TEXT NOT NULL,
    uncommitted_etc TEXT,
    etc             TEXT NOT NULL,
    eac             TEXT NOT NULL,
    revenue         TEXT NOT NULL,
    profit          TEXT NOT NULL,
    complete        INTEGER NOT NULL CHECK (complete IN (0, 1)),
    assumptions_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(assumptions_json)),
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_forecast_snapshots ON forecast_snapshots(project_id, as_of);
