"""Sekiz standart rapor: kayıt (payload) ya da servis verisinden ReportDoc üretir."""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

from yapimmaliyet.domain.analysis import AnalysisResult, AnalysisSpec, ItemType
from yapimmaliyet.domain.calc import PriceOrigin
from yapimmaliyet.domain.firm_cost import COMPONENT_LABELS, COMPONENTS
from yapimmaliyet.domain.parsing import format_date_tr
from yapimmaliyet.domain.periods import UNSPECIFIED, date_tr
from yapimmaliyet.domain.tender import TENDER_MODELS
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table, dec, fmt

SEVERITY = {"HATA": "Hata", "UYARI": "Uyarı", "BILGI": "Bilgi"}


def _meta(p: dict) -> list[tuple[str, str]]:
    m = [("Proje", p["project"]["name"])]
    if p["project"].get("ikn"):
        m.append(("İKN", p["project"]["ikn"]))
    if p["project"].get("authority"):
        m.append(("İdare", p["project"]["authority"]))
    m.append(("Keşif", f"{p['estimate']['name']} (Rev. {p['estimate']['revision']})"))
    m.append(("Hesap türü", p["kind_label"]))
    groups = source_groups(p)
    if len(groups) == 1:
        (auth, period, valid), _ = next(iter(groups.items()))
        m.append(("Fiyat Kaynağı", f"{auth} — Dönem: {period or UNSPECIFIED} — Geçerlilik: {date_tr(valid)}"))
    elif groups:
        m.append(("Fiyat Kaynakları", f"Karma — {len(groups)} kurum/dönem (ayrıntı: Fiyat kaynakları dökümü)"))
    if p.get("tender"):
        m.append(("İhale modeli", p["tender"]["model_label"]))
    if "profile" in p:
        pr = p["profile"]
        m.append(("Hesap profili", f"{pr['name']} s.{pr['version']} — "
                                   f"{'doğrulanmış' if pr['verified'] else 'DOĞRULANMAMIŞ'}"))
    if p.get("snapshot_id"):
        m.append(("Kesinleşmiş kayıt", f"No {p['snapshot_id']} · {format_date_tr(p['created_at'])}"
                                       + (f" · {p['name']}" if p.get("name") else "")))
    else:
        m.append(("Durum", "TASLAK — kesinleşmemiş hesap; nihai rapor değildir"))
    m.append(("Uygulama sürümü", p["app_version"]))
    return m


def source_groups(p: dict) -> OrderedDict[tuple[str, str | None, str | None], list]:
    """Fiyatlı kalemlerin kurum / dönem / geçerlilik grupları: [kalem sayısı, tutar]. Eski kesinleşmiş kayıtlarda kurum ve
    dönem alanı yoksa kurum kaynak metninden alınır, dönem 'Belirtilmemiş' kalır (uydurulmaz)."""
    out: OrderedDict[tuple[str, str | None, str | None], list] = OrderedDict()
    for ln in p.get("lines", []):
        if ln["origin"] == "YOK":
            continue
        if ln["origin"] == "KITAP":
            auth = ln.get("price_authority") or (ln["price_source"] or "").split(" — ")[0] or UNSPECIFIED
            date = ln.get("price_date") or ""
            key = (auth, ln.get("price_period"), date if len(date) == 10 else None)
        else:
            key = ("Özel poz / analiz", None, None)
        g = out.setdefault(key, [0, Decimal(0)])
        g[0] += 1
        g[1] += dec(ln["amount"]) or Decimal(0)
    return out


def source_section(p: dict) -> Section | None:
    """Birden çok kurum ya da dönem kullanılmışsa döküm (tek kaynakta başlık satırı yeterlidir)."""
    groups = source_groups(p)
    if len(groups) < 2:
        return None
    return Section("Fiyat kaynakları dökümü (karma)", Table(
        [Col("Kurum / kaynak", width=2.5), Col("Fiyat dönemi", width=1.5), Col("Geçerlilik", width=1.2),
         Col("Kalem", ColKind.INT, 0.8), Col("Tutar (TL)", ColKind.MONEY, 1.5)],
        [[k[0], k[1] or UNSPECIFIED, date_tr(k[2]), v[0], v[1]] for k, v in groups.items()]))


def _origin(code: str) -> str:
    return PriceOrigin(code).label


def _totals_section(p: dict) -> Section:
    t = p["totals"]
    missing = sum(1 for ln in p.get("lines", []) if ln.get("amount") is None)
    tag = f" — EKSİK FİYATLI ÖN TOPLAM ({missing} satır fiyatsız)" if missing else ""
    rows = [["Ara toplam" + tag, dec(t["subtotal"])]]
    if t.get("overhead_profit") and dec(t["overhead_profit"]):
        rows.append(["Kâr ve genel gider", dec(t["overhead_profit"])])
    if p["kind"] == "YAKLASIK":
        rows.append(["KDV hariç toplam" + tag, dec(t["total_excl_vat"])])
        if t.get("vat"):
            rows += [["KDV (bilgi amaçlı)", dec(t["vat"])], ["KDV dahil toplam", dec(t["total_incl_vat"])]]
    paragraphs = []
    fx = p.get("currency")
    if fx:   # TL temel; döviz karşılığı yalnızca bilgi amaçlı ve sabitlenen kurla
        rows.append([f"KDV hariç toplamın {fx['currency']} karşılığı (bilgi) — tutar {fx['currency']} cinsindendir",
                     dec(fx["total_excl_vat"])])
        src = "TCMB" if fx["source"] == "TCMB" else "ELLE GİRİLEN KUR"
        paragraphs.append(f"Kur: 1 {fx['currency']} = {fx['rate']} TL ({src}, {fx['rate_type']}, kur tarihi "
                          f"{format_date_tr(fx['rate_date'])}, kayıt zamanı {fx['fetched_at']}). Kur keşfe "
                          "sabitlenmiştir; sonradan çekilen kurlar bu hesabı değiştirmez.")
    return Section("Toplamlar", Table([Col("Kalem", width=3), Col("Tutar (TL)", ColKind.MONEY, 1.5)], rows),
                   paragraphs)


def _tender_sections(p: dict) -> list[Section]:
    """Disiplin ve teklif yöntemi dökümü (eski kayıtlarda bu bilgi yoksa boş)."""
    t = p.get("tender")
    if not t:
        return []
    cols = [Col("Kısım", width=3), Col("Kalem", ColKind.INT, 0.8), Col("Fiyatsız", ColKind.INT, 0.8),
            Col("Tutar (TL)", ColKind.MONEY, 1.5)]
    out = []
    for title, key in (("Disiplin dökümü", "by_discipline"), ("Teklif yöntemi dökümü", "by_method")):
        rows = [[x["label"], x["count"], x["unpriced"], dec(x["amount"])] for x in t[key]]
        tot = ["TOPLAM", sum(x["count"] for x in t[key]), sum(x["unpriced"] for x in t[key]),
               sum((dec(x["amount"]) for x in t[key]), Decimal(0))]
        out.append(Section(f"{title} — {t['model_label']}", Table(cols, rows, tot)))
    return out


def estimate_list(p: dict) -> ReportDoc:
    cols = [Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("İmalat", width=4), Col("Birim", width=0.6),
            Col("Miktar", ColKind.QTY, 1), Col("Birim Fiyat", ColKind.PRICE, 1), Col("Tutar (TL)", ColKind.MONEY, 1.2),
            Col("Fiyat kaynağı", width=2.2), Col("Not", width=1)]
    rows = [[ln["line_no"], ln["code"], ln["description"], ln["unit"], dec(ln["quantity"]), dec(ln["unit_price"]),
             dec(ln["amount"]), ln["price_source"], "Kullanıcı değiştirdi" if ln["overridden"] else
             ("Birim farkı kabul" if ln.get("unit_mismatch_accepted") else "")] for ln in p["lines"]]
    total = [None, None, "TOPLAM", None, None, None, dec(p["totals"]["subtotal"]), None, None]
    sections = [Section("Keşif kalemleri", Table(cols, rows, total)), *_tender_sections(p), _totals_section(p)]
    index = _index_section(p)
    if index:
        sections.insert(1, index)
    if (mixed := source_section(p)) is not None:
        sections.insert(1, mixed)
    meta = _meta(p)
    if p["estimate"].get("ym_date"):
        meta.append(("Yaklaşık maliyet hesap tarihi", format_date_tr(p["estimate"]["ym_date"])))
    return ReportDoc("Poz Bazlı Keşif Listesi", meta, sections, p["notes"] + _tender_notes(p),
                     draft=not p.get("snapshot_id"))


def _tender_notes(p: dict) -> list[str]:
    return list(p["tender"]["notes"]) if p.get("tender") else []


def _index_section(p: dict) -> Section | None:
    """Endeksle güncellenen satırların dökümü: temel fiyat, dönemler, endeks değerleri ve katsayı."""
    lines = [ln for ln in p["lines"] if ln.get("index")]
    if not lines:
        return None
    cols = [Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("Temel fiyat", ColKind.PRICE, 1.1),
            Col("Endeks", width=0.9), Col("Temel dönem", width=0.9), Col("Temel endeks", width=1),
            Col("Güncel dönem", width=0.9), Col("Güncel endeks", width=1), Col("Katsayı", width=1),
            Col("Güncel fiyat", ColKind.PRICE, 1.1)]
    rows = [[ln["line_no"], ln["code"], dec(i["base_price"]), i["series"], i["base_period"], i["base_value"],
             i["current_period"], i["current_value"], f"{dec(i['factor']):.6f}".replace(".", ","),
             dec(ln["unit_price"])] for ln in lines for i in (ln["index"],)]
    return Section("Endeks güncellemesi (birim fiyat × güncel endeks / temel endeks)", Table(cols, rows))


def group_summary(p: dict) -> ReportDoc:
    cols = [Col("İş grubu", width=4), Col("Kalem sayısı", ColKind.INT, 1), Col("Tutar (TL)", ColKind.MONEY, 1.5),
            Col("Pay", ColKind.PERCENT, 1)]
    sub = dec(p["totals"]["subtotal"]) or Decimal(0)
    rows = [[g["name"], g["count"], dec(g["amount"]), (dec(g["amount"]) / sub) if sub else None] for g in p["groups"]]
    total = ["TOPLAM", sum(g["count"] for g in p["groups"]), sub, Decimal(1) if sub else None]
    return ReportDoc("İş Grubu Bazlı İcmal", _meta(p), [Section("İcmal", Table(cols, rows, total)),
                                                         *_tender_sections(p), _totals_section(p)],
                     p["notes"] + _tender_notes(p),
                     draft=not p.get("snapshot_id"), landscape=False)


def price_sources(p: dict) -> ReportDoc:
    agg: OrderedDict[tuple[str, str], list] = OrderedDict()
    for ln in p["lines"]:
        key = (ln["price_source"] or "(kaynak yok)", ln["price_date"] or "")
        a = agg.setdefault(key, [0, Decimal(0), ln["origin"]])
        a[0] += 1
        a[1] += dec(ln["amount"]) or Decimal(0)
    summary = Table([Col("Fiyat kaynağı", width=4), Col("Fiyat tarihi", width=1.2), Col("Yöntem", width=2),
                     Col("Kalem", ColKind.INT, 0.8), Col("Tutar (TL)", ColKind.MONEY, 1.5)],
                    [[k[0], format_date_tr(k[1]), _origin(v[2]), v[0], v[1]] for k, v in agg.items()])
    detail = Table([Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.3), Col("İmalat", width=3.5),
                    Col("Kaynak birim fiyatı", ColKind.PRICE, 1.2), Col("Kullanılan birim fiyat", ColKind.PRICE, 1.2),
                    Col("Kaynak", width=2.5), Col("Tarih", width=1), Col("Yöntem", width=1.6)],
                   [[ln["line_no"], ln["code"], ln["description"], dec(ln.get("matched_price")), dec(ln["unit_price"]),
                     ln["price_source"], format_date_tr(ln["price_date"]), _origin(ln["origin"])
                     + (" · kullanıcı değiştirdi" if ln["overridden"] else "")] for ln in p["lines"]])
    sections = [Section("Kaynak özeti", summary), Section("Kalem bazında fiyat kaynakları", detail)]
    if (mixed := source_section(p)) is not None:
        sections.insert(0, mixed)
    return ReportDoc("Birim Fiyat Kaynak Dökümü", _meta(p), sections,
                     p["notes"], draft=not p.get("snapshot_id"))


def analysis_report(cp, entries: list[tuple[object, AnalysisSpec | None, AnalysisResult | None]]) -> ReportDoc:
    meta = [("Özel poz", f"{cp.code} — {cp.name}"), ("Uyarı", "Kullanıcı tarafından oluşturulmuştur; "
                                                               "resmî birim fiyat değildir.")]
    if cp.category:
        meta.append(("Kategori", cp.category))
    sections = []
    for v, spec, res in entries:
        paras = [f"Birim: {v.unit} · Fiyat tarihi: {format_date_tr(v.price_date)} · Yöntem: {v.price_method.label} · "
                 f"Birim fiyat: {fmt(v.effective_price, ColKind.PRICE)} TL" + (" · KİLİTLİ" if v.locked else ""),
                 f"Tarif: {v.description}", f"Fiyat dayanağı: {v.basis_text}"]
        table = None
        if spec and res:
            paras += res.explanation
            table = Table([Col("Tür", width=1.2), Col("Açıklama", width=3.5), Col("Birim", width=0.7),
                           Col("Miktar", ColKind.QTY, 0.9), Col("Katsayı", ColKind.QTY, 0.8),
                           Col("Dönüşüm", ColKind.QTY, 0.8), Col("Fire", ColKind.PERCENT, 0.7),
                           Col("Birim fiyat", ColKind.PRICE, 1), Col("Tutar (TL)", ColKind.MONEY, 1),
                           Col("Kaynak", width=1.8)],
                          [[ItemType(ln.item.item_type).label, ln.item.description, ln.item.unit, ln.item.quantity,
                            ln.item.coefficient, ln.item.unit_conversion, ln.item.waste_rate, ln.item.unit_price,
                            ln.amount, ln.item.source_text] for ln in res.lines],
                          [None, "ARA TOPLAM", None, None, None, None, None, None, res.subtotal, None])
        sections.append(Section(f"Sürüm {v.version_no}", table, paras))
    return ReportDoc("Özel Poz Analiz Raporu", meta, sections,
                     ["Genel gider ve kâr oranları kullanıcı tarafından belirlenmiştir; mevzuat varsayımı değildir."])


def _offer_meta(p: dict) -> list[tuple[str, str]]:
    m = _meta(p)
    m.insert(4, ("Maliyet çalışması", p["sheet"]["name"]))
    return m


def firm_cost(p: dict) -> ReportDoc:
    cols = ([Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.2), Col("İmalat", width=3), Col("Birim", width=0.6),
             Col("Miktar", ColKind.QTY, 0.9)] + [Col(COMPONENT_LABELS[k], ColKind.PRICE, 0.9) for k in COMPONENTS]
            + [Col("Doğrudan tutar", ColKind.MONEY, 1.1), Col("Dolaylı pay", ColKind.MONEY, 1),
               Col("Toplam maliyet", ColKind.MONEY, 1.1)])
    rows = [[c["line_no"], c["code"], c["description"], c["unit"], dec(c["quantity"])]
            + [dec(c["components"][k]) for k in COMPONENTS]
            + [dec(c["direct_amount"]), dec(c["indirect_amount"]), dec(c["total_cost"])] for c in p["cost_lines"]]
    t = p["cost_totals"]
    total = [None, None, "TOPLAM", None, None] + [None] * len(COMPONENTS) + [dec(t["direct"]), dec(t["indirect"]),
                                                                            dec(t["total"])]
    ind = Table([Col("Dolaylı gider", width=3), Col("Yöntem", width=1.5), Col("Değer", width=1),
                 Col("Tutar (TL)", ColKind.MONEY, 1.2), Col("Not", width=2)],
                [[i["name"], "Sabit tutar" if i["method"] == "SABIT" else "Doğrudan maliyete oran",
                  fmt(dec(i["value"]), ColKind.MONEY if i["method"] == "SABIT" else ColKind.PERCENT),
                  dec(i["amount"]), i["note"]] for i in p["indirects"]],
                [ "TOPLAM", None, None, dec(t["indirect"]), None])
    return ReportDoc("Firma Maliyeti Raporu", _offer_meta(p),
                     [Section("Kalem bazında maliyet (birim başına bileşenler)", Table(cols, rows, total)),
                      Section("Dolaylı giderler (doğrudan tutar oranında dağıtılır)", ind)],
                     p["notes"], draft=not p.get("snapshot_id"))


def offer_comparison(p: dict) -> ReportDoc:
    ref = dec(p["reference"]["total"])
    summary = Table([Col("Senaryo", width=2), Col("Kâr", ColKind.PERCENT, 0.8), Col("Risk", ColKind.PERCENT, 0.8),
                     Col("Maliyet (TL)", ColKind.MONEY, 1.3), Col("Teklif (TL)", ColKind.MONEY, 1.3),
                     Col("Kâr tutarı (TL)", ColKind.MONEY, 1.3), Col("Kârlılık", ColKind.PERCENT, 0.9),
                     Col("Karşılaştırma tabanına oran", ColKind.PERCENT, 1.2), Col("Not", width=2)],
                    [[o["name"], dec(o["profit_rate"]), dec(o["risk_rate"]), dec(p["cost_totals"]["total"]),
                      dec(o["offer_total"]), dec(o["profit_amount"]), dec(o["margin"]), dec(o["ratio_to_reference"]),
                      o["notes"]] for o in p["offers"]])
    cols =[Col("Sıra", ColKind.INT, 0.5), Col("Poz No", width=1.2), Col("İmalat", width=3), Col("Miktar", ColKind.QTY, 0.9),
            Col("Birim maliyet", ColKind.PRICE, 1)] + [Col(f"{o['name']} birim fiyat", ColKind.PRICE, 1) for o in p["offers"]]
    rows = []
    for c in p["cost_lines"]:
        q = dec(c["quantity"])
        unit_cost = (dec(c["total_cost"]) / q) if q else dec(c["direct_unit"])
        row = [c["line_no"], c["code"], c["description"], q, unit_cost.quantize(Decimal("0.0001")) if unit_cost is not None else None]
        for o in p["offers"]:
            row.append(dec(next(x["unit_price"] for x in o["lines"] if x["line_no"] == c["line_no"])))
        rows.append(row)
    paras = [f"Karşılaştırma tabanı: {p['reference']['label']}" + (f" ({fmt(ref, ColKind.MONEY)} TL)" if ref else "")]
    return ReportDoc("Alternatif Teklif Karşılaştırması", _offer_meta(p),
                     [Section("Senaryo özeti", summary, paras), Section("Kalem bazında teklif birim fiyatları",
                                                                        Table(cols, rows))],
                     p["notes"], draft=not p.get("snapshot_id"))


def import_issues(job, issues) -> ReportDoc:
    meta = [("İş no", str(job.id)), ("Tür", "Birim fiyat kitabı" if job.kind == "FIYAT_KITABI" else "Keşif listesi"),
            ("Dosya", job.file_name or ""), ("Hedef", job.target or ""),
            ("Tarih", format_date_tr(job.finished_at or job.started_at))]
    stats = Table([Col("Gösterge", width=2), Col("Değer", width=1)], [[k, str(v)] for k, v in job.stats.items()])
    rows = [[i["location"], SEVERITY.get(i["severity"], i["severity"]), i["message"], i["resolution"]] for i in issues]
    return ReportDoc("İçe Aktarma Hata Raporu", meta,
                     [Section("Özet", stats), Section("Satır bazında sorunlar", Table(
                         [Col("Konum", width=1.3), Col("Önem", width=0.7), Col("Açıklama", width=5),
                          Col("Sonuç", width=1.5)], rows))])


def project_summary(project, estimates, snapshots: list[tuple[object, dict]]) -> ReportDoc:
    meta = [("Proje", project.name), ("İKN", project.ikn or ""), ("İdare", project.authority_name or ""),
            ("İhale tarihi", format_date_tr(project.tender_date)),
            ("İhale modeli", TENDER_MODELS.get(project.tender_model, project.tender_model))]
    est = Table([Col("Keşif", width=2.5), Col("Kalem", ColKind.INT, 0.7), Col("Onaylı", ColKind.INT, 0.7),
                 Col("Kilitli", width=0.7)],
                [[e.display_name, e.item_count, e.approved_count, "Evet" if e.locked else "Hayır"] for e in estimates])
    rows = []
    for info, pl in snapshots:
        total = pl.get("totals", {}).get("total_excl_vat") or pl.get("cost_totals", {}).get("total")
        rows.append([info.id, format_date_tr(info.created_at), pl["kind_label"], pl["estimate"]["name"],
                     info.name, info.profile_name, dec(total)])
    snaps = Table([Col("Kayıt", ColKind.INT, 0.6), Col("Tarih", width=0.9), Col("Tür", width=2.2), Col("Keşif", width=1.5),
                   Col("Ad", width=1.5), Col("Profil", width=2.2), Col("Toplam (TL, KDV hariç / maliyet)", ColKind.MONEY, 1.5)],
                  rows)
    return ReportDoc("Proje Hesaplama Özeti", meta, [Section("Keşif listeleri", est),
                                                     Section("Kesinleşmiş hesaplar", snaps)],
                     ["Bu özet yalnızca kesinleşmiş (değişmez) kayıtları listeler. Sonuçlar ihale dokümanı ve "
                      "yürürlükteki mevzuata göre kontrol edilmelidir."])
