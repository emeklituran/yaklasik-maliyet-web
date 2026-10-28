-- 0004: Keşif kaynak önceliği, analiz kâr tabanı ve başlangıç hesap profilleri.
ALTER TABLE estimates ADD COLUMN source_priority_json TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(source_priority_json));
ALTER TABLE analyses ADD COLUMN profit_base TEXT NOT NULL DEFAULT 'ARA_TOPLAM'
    CHECK (profit_base IN ('ARA_TOPLAM','ARA_TOPLAM_VE_GG'));

-- Referans profili mevzuat içermez (yalnızca toplama); doğrulanmış sayılır.
INSERT INTO calc_profiles(kind, name, version, params_json, legal_basis_text, verified)
VALUES ('REFERANS', 'Referans fiyat', 1,
        '{"rounding":{"quantity_places":3,"price_places":2,"amount_places":2,"analysis_places":6,"mode":"HALF_UP"}}',
        'Seçilen birim fiyatlarla keşif toplamı. Gider, kâr ya da vergi eklenmez.', 1);

-- Örnek profil: varsayımlar mevzuat kuralı değildir; kullanıcı tarafından doğrulanmalıdır.
INSERT INTO calc_profiles(kind, name, version, params_json, legal_basis_text, verified)
VALUES ('YAKLASIK', 'İdare esaslı tahmini yaklaşık maliyet (örnek)', 1,
        '{"rounding":{"quantity_places":3,"price_places":2,"amount_places":2,"analysis_places":6,"mode":"HALF_UP"},"prices_include_overhead_profit":true,"overhead_profit_rate":"0.25","vat_rate":"0.20"}',
        'DOĞRULANMAMIŞ ÖRNEK PROFİL. Varsayımlar: (1) Bakanlık birim fiyatlarının %25 yüklenici kârı ve genel gider içerdiği yaygın uygulamaya dayanır; bu nedenle ayrıca eklenmez. (2) Yaklaşık maliyet KDV hariçtir; KDV %20 varsayılarak yalnızca bilgi amaçlı gösterilir. Yapım İşleri İhaleleri Uygulama Yönetmeliği, Kamu İhale Genel Tebliği, ihale dokümanı ve güncel düzenlemelere göre doğrulanmalıdır.',
        0);
