-- 0010: Birim fiyat kitaplarındaki ORAN pozları (Ölçü: %), ör. 25.300.2100 "Bina içinde vidalı döşenmiş boru montaj
-- malzemesi bedeli %30". Fiyat değil orandır; position_prices'a yazılmaz (yanlışlıkla fiyat gibi kullanılmasın).
-- Kitapla birlikte silinir. Oran kaynaktaki gibi saklanır; nasıl uygulanacağına kullanıcı karar verir.

CREATE TABLE rate_positions (
    id            INTEGER PRIMARY KEY,
    price_book_id INTEGER NOT NULL REFERENCES price_books(id) ON DELETE CASCADE,
    code          TEXT NOT NULL CHECK (length(trim(code)) > 0),
    code_norm     TEXT NOT NULL,
    description   TEXT NOT NULL,
    rate          TEXT NOT NULL CHECK (rate GLOB '[0-9]*' AND rate NOT GLOB '*[^0-9.]*'),   -- yüzde: "30" = %30
    source_page   INTEGER,
    import_job_id INTEGER REFERENCES import_jobs(id) ON DELETE SET NULL,
    UNIQUE (price_book_id, code_norm)
);

CREATE INDEX ix_rate_positions_code ON rate_positions(code_norm);
