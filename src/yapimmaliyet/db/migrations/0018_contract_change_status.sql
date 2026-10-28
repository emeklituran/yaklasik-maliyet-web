-- 0018: Sözleşme değişikliğinde onay durumu. Yalnızca ONAYLI kayıtlar güncel sözleşme bedeline ve süresine yansır;
-- TASLAK kayıt (onay bekleyen iş artışı, süre uzatımı talebi vb.) izlenir ama hesaba girmez.
-- Mevcut kayıtlar onay tarihi / sayısıyla girildiği için ONAYLI kabul edilir (varsayılan değer).

ALTER TABLE pp_contract_changes ADD COLUMN status TEXT NOT NULL DEFAULT 'ONAYLI'
    CHECK (status IN ('TASLAK', 'ONAYLI'));
