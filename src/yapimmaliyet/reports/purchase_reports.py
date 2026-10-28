"""Satın alma fiyat mukayesesi ve onay raporu."""

from __future__ import annotations

from datetime import datetime

from yapimmaliyet import __version__
from yapimmaliyet.domain.purchase import Evaluation
from yapimmaliyet.reports.model import Col, ColKind, ReportDoc, Section, Table


def purchase_comparison(req, ev: Evaluation, criteria: list[tuple[int, str, object]],
                        scores: dict[tuple[int, int], object], selected_label: str | None) -> ReportDoc:
    offers = ev.offers
    price_cols = [Col("Sıra", ColKind.INT, 0.4), Col("Kalem", width=2.6), Col("Birim", width=0.6),
                  Col("İstenen miktar", ColKind.QTY, 0.9)]
    for o in offers:
        price_cols += [Col(f"{o.offer.firm} net B.F.", ColKind.PRICE, 1), Col(f"{o.offer.firm} tutar (normalize)",
                                                                               ColKind.MONEY, 1.1)]
    rows = []
    for it in ev.items:
        row = [it.line_no, (f"[{it.pos_code}] " if it.pos_code else "") + it.description
               + (f" — {it.spec}" if it.spec else ""), it.unit, it.quantity]
        for o in offers:
            ln = next((x for x in o.offer.lines if x.item_id == it.id), None)
            row += [ln.net_unit_price if ln else None, o.normalized_lines.get(it.id)]
        rows.append(row)
    cost = Table([Col("Firma", width=2)] + [Col(t, ColKind.MONEY, 1) for t in (
        "Ara toplam", "İskonto", "Nakliye", "Montaj", "Diğer", "KDV hariç", "KDV", "KDV dahil", "Normalize KDV hariç")],
        [[o.offer.firm, o.subtotal, o.discount_total, o.offer.shipping, o.offer.installation, o.offer.other_costs,
          o.total_excl_vat, o.vat, o.total_incl_vat, o.normalized_excl_vat] for o in offers])
    terms = Table([Col("Firma", width=1.6), Col("Teklif tarihi", width=0.9), Col("Geçerlilik", width=0.9),
                   Col("Teslim (gün)", width=0.7), Col("Ödeme", width=1.4), Col("Garanti", width=1.2),
                   Col("Teknik", width=0.8), Col("Eksiksiz", width=0.6), Col("Uyarılar", width=3)],
                  [[o.offer.firm, o.offer.offer_date or "", o.offer.valid_until or "",
                    "" if o.offer.delivery_days is None else str(o.offer.delivery_days), o.offer.payment_terms,
                    o.offer.warranty, {True: "Uygun", False: "Uygun değil", None: "—"}[o.offer.technical_ok],
                    "Evet" if o.complete else "Hayır", " ".join(o.warnings)] for o in offers])
    score_t = Table([Col("Ölçüt", width=2), Col("Ağırlık", ColKind.QTY, 0.7)] +
                    [Col(o.offer.firm, ColKind.QTY, 1) for o in offers],
                    [[n, w] + [scores.get((cid, o.offer.id)) for o in offers] for cid, n, w in criteria] +
                    [["Ağırlıklı puan (bilgi)", None] + [o.weighted_score for o in offers]])
    decision = [f"Seçilen teklif: {selected_label or 'henüz karar verilmedi'}",
                f"Seçim gerekçesi: {req.decision_reason or '—'}",
                f"Değerlendiren: {req.evaluator or '……………………'}   İmza: ……………",
                f"Onaylayan: {req.approver or '……………………'}   İmza: ……………   Tarih: {req.decided_at or '……………'}"]
    meta = [("Satın alma konusu", req.title), ("Teknik kapsam", req.scope or "—"), ("Revizyon", str(req.revision)),
            ("Durum", {"TASLAK": "Değerlendirmede", "ONAYLANDI": "Onaylandı", "IPTAL": "İptal"}[req.status]),
            ("Oluşturma", datetime.now().strftime("%d.%m.%Y %H:%M")), ("Uygulama sürümü", __version__)]
    return ReportDoc("Satın Alma Fiyat Mukayesesi ve Onay", meta, [
        Section("Kalem bazında fiyat karşılaştırması", Table(price_cols, rows)),
        Section("Maliyet bileşenleri (TL)", cost), Section("Teslim, ödeme, garanti ve teknik uygunluk", terms),
        Section("Değerlendirme ölçütleri", score_t), Section("Karar ve onay", paragraphs=decision)], ev.notes)
