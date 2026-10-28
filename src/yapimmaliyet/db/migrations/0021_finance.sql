-- Finans modülleri: kasa ve banka hesapları, cari hesap hareketleri, çek ve senetler, kredi ve borç yönetimi.
-- Cari kartı ayrı tutulmaz: Firmalar ve Taraflar (parties) kaydı cari karttır.
-- Tutarlar kanonik metin (2 basamak). Hesap bakiyesi = açılış + hareketlerin toplamı (fin_moves.amount işaretli).

CREATE TABLE fin_accounts (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
    kind       TEXT NOT NULL CHECK (kind IN ('KASA', 'BANKA')),
    bank       TEXT NOT NULL DEFAULT '',
    iban       TEXT NOT NULL DEFAULT '',
    opening    TEXT NOT NULL DEFAULT '0.00',
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

-- Hesap hareketleri: giriş (+) / çıkış (−). source: ELLE, CARI, VIRMAN, SENET, BORC
CREATE TABLE fin_moves (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES fin_accounts(id) ON DELETE RESTRICT,
    move_date   TEXT NOT NULL,
    amount      TEXT NOT NULL,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    source      TEXT NOT NULL DEFAULT 'ELLE' CHECK (source IN ('ELLE', 'CARI', 'VIRMAN', 'SENET', 'BORC')),
    group_key   TEXT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_fin_moves_account ON fin_moves(account_id, move_date);

-- Cari hareketler. BORC: cariyi borçlandırır (fatura kestik), ALACAK: cariyi alacaklandırır (fatura aldık),
-- TAHSILAT: cariden para aldık, ODEME: cariye para verdik. Bakiye > 0 → cari borçlu.
CREATE TABLE cari_entries (
    id          INTEGER PRIMARY KEY,
    party_id    INTEGER NOT NULL REFERENCES parties(id) ON DELETE RESTRICT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    entry_date  TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('BORC', 'ALACAK', 'TAHSILAT', 'ODEME')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    document_no TEXT NOT NULL DEFAULT '',
    amount      TEXT NOT NULL,
    due_date    TEXT,
    move_id     INTEGER REFERENCES fin_moves(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_cari_entries_party ON cari_entries(party_id, entry_date);

CREATE TABLE fin_notes (
    id         INTEGER PRIMARY KEY,
    direction  TEXT NOT NULL CHECK (direction IN ('ALINAN', 'VERILEN')),
    kind       TEXT NOT NULL CHECK (kind IN ('CEK', 'SENET')),
    party_id   INTEGER REFERENCES parties(id) ON DELETE SET NULL,
    number     TEXT NOT NULL DEFAULT '',
    bank       TEXT NOT NULL DEFAULT '',
    amount     TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    due_date   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'PORTFOY'
               CHECK (status IN ('PORTFOY', 'TAHSIL', 'ODENDI', 'CIRO', 'KARSILIKSIZ', 'IADE')),
    move_id    INTEGER REFERENCES fin_moves(id) ON DELETE SET NULL,
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_fin_notes_due ON fin_notes(status, due_date);

-- Krediler ve borçlar: KREDI (eşit taksitli), KART (kredi kartı), KMH (kredili mevduat), ELDEN (kişi/kurum borcu)
CREATE TABLE fin_debts (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('KREDI', 'KART', 'KMH', 'ELDEN')),
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    lender          TEXT NOT NULL DEFAULT '',
    principal       TEXT NOT NULL,
    balance         TEXT NOT NULL,
    annual_rate     TEXT NOT NULL DEFAULT '0.0000',
    monthly_payment TEXT NOT NULL DEFAULT '0.00',
    credit_limit    TEXT NOT NULL DEFAULT '0.00',
    pay_day         INTEGER NOT NULL DEFAULT 1 CHECK (pay_day BETWEEN 1 AND 31),
    start_date      TEXT NOT NULL,
    notes           TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE fin_debt_payments (
    id         INTEGER PRIMARY KEY,
    debt_id    INTEGER NOT NULL REFERENCES fin_debts(id) ON DELETE CASCADE,
    pay_date   TEXT NOT NULL,
    amount     TEXT NOT NULL,
    interest   TEXT NOT NULL,
    principal  TEXT NOT NULL,
    move_id    INTEGER REFERENCES fin_moves(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_fin_debt_payments_debt ON fin_debt_payments(debt_id, pay_date);
