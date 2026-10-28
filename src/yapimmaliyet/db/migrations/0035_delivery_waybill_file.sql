-- İrsaliyenin orijinal taraması / PDF'i: teslim kaydına (irsaliye no) bağlı dosya. Dosya veri klasörüne kopyalanır
-- (KaynakDosyalar/Irsaliyeler), burada veri klasörüne göre göreli yolu tutulur. Mevcut teslimler dosyasız kalır.
ALTER TABLE po_deliveries ADD COLUMN waybill_file TEXT;
