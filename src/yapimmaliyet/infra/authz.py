"""Oturumdaki kullanıcı ve işlem yetkisi denetimi (isteğe bağlı: kullanıcı tanımlanmamışsa kısıt yoktur).

Denetim işlem katmanında yapılır: bütün servis yazma işlemleri `audit.log` çağırır; `audit.log` önce `check_entity` ile yetkiyi
sınar, yetki yoksa `PermissionDenied` fırlatır ve işlemin transaction'ı geri alınır. Onay işlemleri (hakediş onayı, satın alma
kararı, teklif seçimi, ihale kazanma, sözleşme değişikliği onayı) ayrıca `require(..., APPROVE)` ister. Menü gizleme tek başına
güvenlik sayılmaz. Oturum yoksa (kullanıcı tanımlı değil, test, betik) denetim yapılmaz.

Seviyeler kümülatiftir: 1 Görüntüleme, 2 Oluşturma, 3 Düzenleme, 4 Onaylama, 5 Silme/İptal, 6 Yönetim.
Güvenlik sınırı: yerel SQLite dosyası olduğundan bu bir işlem kontrolüdür; dosyaya doğrudan erişimi olan biri uygulamayı atlayabilir.
"""

from __future__ import annotations

from dataclasses import dataclass

from yapimmaliyet.infra.errors import AppError

VIEW, CREATE, EDIT, APPROVE, DELETE, ADMIN = 1, 2, 3, 4, 5, 6
LEVELS = {0: "Yok", VIEW: "Görüntüleme", CREATE: "Oluşturma", EDIT: "Düzenleme", APPROVE: "Onaylama", DELETE: "Silme / iptal",
          ADMIN: "Yönetim"}

MODULES = {
    "PROJE": "Projeler ve ihaleler", "FIRMA": "Firmalar ve cari kartları", "BIRIM_FIYAT": "Birim fiyatlar ve analizler",
    "KESIF": "Keşif ve yaklaşık maliyet", "TEKLIF": "Teklif ve alt yükleniciler", "SOZLESME": "Sözleşmeler ve iş programı",
    "HAKEDIS": "Hakedişler", "SATIN_ALMA": "Satın alma, sipariş ve stok", "MALIYET": "Maliyet, bütçe ve faaliyet planı",
    "FINANS": "Finans (cari, kasa, çek/senet, borç)", "RAPOR": "Raporlar", "VERI": "Veri yönetimi ve yedek",
    "AYAR": "Ayarlar, oranlar ve kullanıcılar",
}

ENTITY_MODULE = {
    "project": "PROJE", "authority": "PROJE", "party": "FIRMA", "price_book": "BIRIM_FIYAT", "position_price": "BIRIM_FIYAT",
    "analysis_book": "BIRIM_FIYAT", "custom_position": "BIRIM_FIYAT", "index_series": "BIRIM_FIYAT",
    "subcode_rule": "BIRIM_FIYAT", "code_candidate": "BIRIM_FIYAT",
    "index_value": "BIRIM_FIYAT", "exchange_rate": "BIRIM_FIYAT", "new_price_record": "BIRIM_FIYAT", "estimate": "KESIF",
    "estimate_item": "KESIF", "calculation": "KESIF", "calc_profile": "KESIF", "estimate_comparison": "KESIF",
    "cost_sheet": "TEKLIF", "tender_bid": "TEKLIF", "sub_package": "TEKLIF", "sub_bid": "TEKLIF", "pp_contract": "SOZLESME",
    "pp_item": "SOZLESME", "pp_contract_change": "SOZLESME", "ws_plan_line": "SOZLESME", "fa_final_account": "SOZLESME",
    "pp_payment": "HAKEDIS", "pp_deduction_type": "HAKEDIS", "purchase_request": "SATIN_ALMA", "purchase_offer": "SATIN_ALMA",
    "po_order": "SATIN_ALMA", "po_delivery": "SATIN_ALMA", "stk_issue": "SATIN_ALMA", "stk_location": "SATIN_ALMA", "stk_move": "SATIN_ALMA", "ac_cost_entry": "MALIYET",
    "ac_budget": "MALIYET", "ws_activity": "MALIYET", "sched_baseline": "MALIYET", "forecast_etc": "MALIYET",
    "forecast_snapshot": "MALIYET", "cari_entry": "FINANS", "fin_note": "FINANS", "fin_move": "FINANS",
    "doc_document": "SOZLESME", "doc_version": "SOZLESME", "doc_decision": "SOZLESME",
    "sub_perf_note": "TEKLIF", "fin_account": "FINANS", "fin_debt": "FINANS", "fin_debt_payment": "FINANS", "ref_rate": "MALIYET",
}
_ACTION_LEVEL = {"EKLE": CREATE, "GUNCELLE": EDIT, "SIL": DELETE, "TOPLU": EDIT}

PAGE_MODULE = {
    "Ana Sayfa": None, "Girilecek İhaleler": "PROJE", "Alınan İşler": "PROJE", "Proje Merkezi": "PROJE",
    "Projeler ve İhaleler": "PROJE", "Firmalar ve Taraflar": "FIRMA", "Birim Fiyat Kütüphanesi": "BIRIM_FIYAT",
    "Fiyat Kitapları": "BIRIM_FIYAT", "Yeni Birim Fiyat Tespiti": "BIRIM_FIYAT", "Analiz Kitapları": "BIRIM_FIYAT",
    "Özel Pozlar ve Analizler": "BIRIM_FIYAT", "Endeksler": "BIRIM_FIYAT", "Keşif Listeleri": "KESIF",
    "Yaklaşık Maliyet": "KESIF", "Mukayeseli Keşif ve İcmal": "KESIF", "Firma Maliyeti ve Teklif": "TEKLIF",
    "Alt Yüklenici Teklifleri": "TEKLIF", "Sözleşmeler ve Değişiklikler": "SOZLESME", "İş Programı ve İlerleme": "SOZLESME",
    "Kesin Hesap": "SOZLESME", "Taşeron Hakedişi": "HAKEDIS", "İdari Hakediş": "HAKEDIS", "Satın Alma Mukayesesi": "SATIN_ALMA",
    "Satın Alma Siparişi ve Teslim": "SATIN_ALMA", "Stok ve Depo": "SATIN_ALMA",
    "Doküman ve Onay Merkezi": "SOZLESME",
    "Gerçekleşen Maliyet ve Nakit Akışı": "MALIYET", "Faaliyet Planı ve Maliyet": "MALIYET", "Cari Hesaplar": "FINANS",
    "Çek ve Senetler": "FINANS", "Kasa ve Banka": "FINANS", "Kredi ve Borç Yönetimi": "FINANS", "Raporlar": "RAPOR",
    "Modül Raporları": "RAPOR", "Veri Yönetimi ve Yedekleme": "VERI", "Oran ve Parametreler": "MALIYET", "Ayarlar": None,
    "Kullanıcılar ve Yetkiler": "AYAR",
}


class PermissionDenied(AppError):
    """Kullanıcının yetkisi olmayan işlem."""


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    display_name: str
    role_key: str
    perms: dict[str, int]

    def level(self, module: str) -> int:
        return self.perms.get(module, 0)


_current: Session | None = None


def activate(session: Session | None) -> None:
    global _current
    _current = session


def current() -> Session | None:
    return _current


def allowed(module: str | None, level: int = VIEW) -> bool:
    """Oturum yoksa her şey serbesttir; modülsüz sayfalar (Ana Sayfa, Ayarlar) herkese açıktır."""
    if _current is None or module is None:
        return True
    return _current.level(module) >= level


def require(module: str, level: int, what: str = "") -> None:
    if not allowed(module, level):
        raise PermissionDenied(
            f"Bu işlem için yetkiniz yok: {MODULES.get(module, module)} — {LEVELS[level]} yetkisi gerekir"
            + (f" ({what})." if what else "."), detail=f"user={_current.username if _current else '-'}")


def check_entity(entity: str, action: str) -> None:
    module = ENTITY_MODULE.get(entity)
    if module is None or _current is None:
        return
    require(module, _ACTION_LEVEL.get(action, EDIT), f"{entity} {action}")
