-- İhale hattı ve alınan iş aşamaları: aynı proje kaydı ihale sürecinden iş sürecine geçer (veri tekrarı yok).
-- tender_status: ihale aşaması (NULL: ihalesiz, doğrudan alınan iş). work_stage: yalnız kazanılmış / alınmış işlerde dolu.
-- Eski `status` sütunu korunur ve aşamalarla birlikte güncellenir.
ALTER TABLE projects ADD COLUMN tender_status TEXT
    CHECK (tender_status IS NULL OR tender_status IN
           ('TAKIP', 'HAZIRLIK', 'TEKLIF_VERILDI', 'SONUC_BEKLENIYOR', 'KAZANILDI', 'KAZANILAMADI', 'IPTAL'));
ALTER TABLE projects ADD COLUMN work_stage TEXT
    CHECK (work_stage IS NULL OR work_stage IN ('KAZANILMIS', 'SOZLESME', 'DEVAM', 'TAMAMLANDI', 'KAPANDI'));
ALTER TABLE projects ADD COLUMN awarded_date TEXT;
ALTER TABLE projects ADD COLUMN stage_note TEXT;

-- Geçiş: taslaklar hazırlık aşamasındaki ihale; devam eden / tamamlanan / arşivlenen projeler alınan iştir.
UPDATE projects SET tender_status = 'HAZIRLIK' WHERE status = 'TASLAK';
UPDATE projects SET work_stage = 'DEVAM' WHERE status = 'DEVAM';
UPDATE projects SET work_stage = 'TAMAMLANDI' WHERE status = 'TAMAMLANDI';
UPDATE projects SET work_stage = 'KAPANDI' WHERE status = 'ARSIV';

CREATE TABLE project_stage_history (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('IHALE', 'IS')),
    stage      TEXT NOT NULL,
    note       TEXT,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_project_stage_history ON project_stage_history(project_id, id);
