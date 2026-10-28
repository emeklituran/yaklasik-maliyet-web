-- Gider kaydını faaliyete bağlama (faaliyet bazlı gerçekleşen maliyet). Bağ isteğe bağlıdır.
ALTER TABLE ac_cost_entries ADD COLUMN activity_id INTEGER REFERENCES ws_activities(id) ON DELETE SET NULL;
CREATE INDEX ix_ac_cost_entries_activity ON ac_cost_entries(activity_id);
