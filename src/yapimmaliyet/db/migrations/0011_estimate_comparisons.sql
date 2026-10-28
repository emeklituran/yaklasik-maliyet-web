-- 0011: Mukayeseli keşif (iki keşif sürümünün karşılaştırması) ve kullanıcının eşleştirme kararları.
-- Karşılaştırma sonucu saklanmaz; her açılışta keşiflerin güncel durumundan hesaplanır. Yalnızca otomatik
-- yapılamayan (belirsiz) eşleştirmelerde kullanıcının onay/ret kararı saklanır.

CREATE TABLE estimate_comparisons (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL CHECK (length(trim(name)) > 0),
    first_estimate_id  INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    second_estimate_id INTEGER NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    profile_id         INTEGER REFERENCES calc_profiles(id) ON DELETE SET NULL,
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    CHECK (first_estimate_id <> second_estimate_id)
);

CREATE TABLE comparison_decisions (
    id             INTEGER PRIMARY KEY,
    comparison_id  INTEGER NOT NULL REFERENCES estimate_comparisons(id) ON DELETE CASCADE,
    first_item_id  INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    second_item_id INTEGER NOT NULL REFERENCES estimate_items(id) ON DELETE CASCADE,
    decision       TEXT NOT NULL CHECK (decision IN ('ONAY', 'RET')),
    note           TEXT,
    decided_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    UNIQUE (comparison_id, first_item_id, second_item_id)
);
