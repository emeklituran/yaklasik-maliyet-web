-- 0019: Firma eşleştirme anahtarında boşluklar da yok sayılır ("A.Ş." = "AŞ" = "A.S.", "Ltd.Şti." = "Ltd. Şti.").
-- Yeni anahtar eski anahtarın boşluksuz hâlidir. Çakışan (artık aynı sayılan) iki kayıttan sonrakinin anahtarı
-- değiştirilmez (OR IGNORE); kayıtlar silinmez, kullanıcı Firmalar sayfasında "Birleştir" ile birleştirebilir.

UPDATE OR IGNORE parties SET name_key = replace(name_key, ' ', '') WHERE instr(name_key, ' ') > 0;
UPDATE OR IGNORE party_aliases SET name_key = replace(name_key, ' ', '') WHERE instr(name_key, ' ') > 0;
