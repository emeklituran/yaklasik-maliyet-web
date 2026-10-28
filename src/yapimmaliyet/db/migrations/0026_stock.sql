-- Stok ve depo: teslim edilen malzeme otomatik stok girişidir (po_delivery_lines); çıkış / sarf kayıtları burada tutulur.
-- Stok = Σ teslim miktarı − Σ çıkış miktarı (sipariş satırı bazında). Maliyet teslimde tanınır; stok yalnız miktar takibidir
-- (çıkış maliyeti yeniden oluşturmaz).
CREATE TABLE stk_issues (
    id            INTEGER PRIMARY KEY,
    order_line_id INTEGER NOT NULL REFERENCES po_order_lines(id) ON DELETE RESTRICT,
    project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    activity_id   INTEGER REFERENCES ws_activities(id) ON DELETE SET NULL,
    issue_date    TEXT NOT NULL,
    quantity      TEXT NOT NULL CHECK (CAST(quantity AS REAL) > 0),
    location      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);
CREATE INDEX ix_stk_issues_line ON stk_issues(order_line_id, issue_date);
