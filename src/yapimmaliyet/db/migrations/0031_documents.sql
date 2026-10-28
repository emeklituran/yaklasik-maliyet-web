-- Doküman ve Onay Merkezi: doküman kaydı, sürümler (ek dosya), karar ve gerekçe. Bağlantı: proje (zorunlu), isteğe bağlı sözleşme /
-- sipariş / satın alma talebi / alt yüklenici paketi ve tedarikçi (firma). Karar kayıtları değiştirilemez; onaylanmış doküman
-- silinemez, onaylı sürümün dosyası değiştirilemez (yeni sürüm eklenir).
CREATE TABLE doc_documents (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind            TEXT NOT NULL CHECK (kind IN ('MALZEME_ONAYI', 'TEKNIK_BELGE', 'SARTNAME', 'SOZLESME_EKI', 'DIGER')),
    title           TEXT NOT NULL CHECK (length(trim(title)) > 0),
    doc_no          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'TASLAK' CHECK (status IN ('TASLAK', 'INCELEMEDE', 'ONAYLI', 'REDDEDILDI', 'REVIZYON')),
    link_type       TEXT CHECK (link_type IS NULL OR link_type IN ('SOZLESME', 'SIPARIS', 'TALEP', 'PAKET')),
    link_id         INTEGER,
    party_id        INTEGER REFERENCES parties(id) ON DELETE SET NULL,
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK ((link_type IS NULL) = (link_id IS NULL))
);
CREATE INDEX ix_doc_documents_project ON doc_documents(project_id, status);

CREATE TABLE doc_versions (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES doc_documents(id) ON DELETE CASCADE,
    version_no  INTEGER NOT NULL CHECK (version_no >= 1),
    file_id     INTEGER REFERENCES source_files(id) ON DELETE RESTRICT,
    file_name   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (document_id, version_no)
);

CREATE TABLE doc_decisions (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES doc_documents(id) ON DELETE CASCADE,
    version_id  INTEGER NOT NULL REFERENCES doc_versions(id) ON DELETE CASCADE,
    decision    TEXT NOT NULL CHECK (decision IN ('ONAY', 'RED', 'REVIZYON')),
    reason      TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    decided_by  TEXT NOT NULL CHECK (length(trim(decided_by)) > 0),
    decided_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_doc_decisions_doc ON doc_decisions(document_id, id);

CREATE TRIGGER trg_doc_decisions_no_update BEFORE UPDATE ON doc_decisions
BEGIN SELECT RAISE(ABORT, 'Karar kaydı değiştirilemez.'); END;
CREATE TRIGGER trg_doc_decisions_no_delete BEFORE DELETE ON doc_decisions
WHEN EXISTS (SELECT 1 FROM doc_documents WHERE id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Karar kaydı silinemez.'); END;

CREATE TRIGGER trg_doc_versions_approved_lock BEFORE UPDATE ON doc_versions
WHEN EXISTS (SELECT 1 FROM doc_decisions WHERE version_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'Karar verilmiş sürüm değiştirilemez; yeni sürüm ekleyin.'); END;
CREATE TRIGGER trg_doc_versions_decided_nodelete BEFORE DELETE ON doc_versions
WHEN EXISTS (SELECT 1 FROM doc_decisions WHERE version_id = OLD.id) AND EXISTS (SELECT 1 FROM doc_documents WHERE id = OLD.document_id)
BEGIN SELECT RAISE(ABORT, 'Karar verilmiş sürüm silinemez.'); END;

CREATE TRIGGER trg_doc_documents_approved_nodelete BEFORE DELETE ON doc_documents
WHEN EXISTS (SELECT 1 FROM doc_decisions WHERE document_id = OLD.id AND decision = 'ONAY')
BEGIN SELECT RAISE(ABORT, 'Onaylanmış doküman silinemez.'); END;
