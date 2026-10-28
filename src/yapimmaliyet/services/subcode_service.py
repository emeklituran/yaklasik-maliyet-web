"""Alt poz (ekli kod) kuralları ve kullanıcı onaylı eşleştirme: '25.300.1103/A' gibi keşif kodları.

Akış: önce tam kod seçili kaynaklarda aranır; resmî kayıt varsa o gösterilir (kural uygulanmaz). Yoksa ana kod aranır,
ekin kuralı (program varsayılanı, kullanıcı değiştirebilir; resmî tarif değildir) öneri olarak gösterilir. Hiçbir öneri
kullanıcı onayı olmadan uygulanmaz; otomatik eşleştirme ekli kodu ana poza bağlamaz.

- ANA_FIYAT_ONERILIR (/A, /B): ana pozun fiyatı önerilir; onayla uygulanır, düzenlenerek de onaylanabilir.
- AYRI_FIYAT_GEREKIR (/D, -D demontaj): fiyat montajdan TÜRETİLMEZ. Dayanak: aynı kodun doğrulanmış DEMONTAJ türündeki
  kitap fiyatı ya da kullanıcının girdiği bedel; ikisi de yoksa onay verilemez.
- Keşifteki özgün kod değişmez; karar, seçilen fiyat, kurum, dönem ve tarih `subcode_decisions` tablosuna yazılır.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from yapimmaliyet.db.connection import transaction
from yapimmaliyet.db.repositories import audit, estimate_items, projects, rate_positions
from yapimmaliyet.domain.matching import split_suffix
from yapimmaliyet.domain.money import from_db_text, to_db_text
from yapimmaliyet.domain.periods import UNVERIFIED, date_tr, text_or_unspecified
from yapimmaliyet.domain.text_tr import normalize_code
from yapimmaliyet.infra.errors import ConflictError, NotFoundError, ValidationError
from yapimmaliyet.services.validators import clean, validate_price

KINDS = {"VIDALI": "Vidalı bağlantı", "KAYNAKLI": "Kaynaklı bağlantı", "DEMONTAJ": "Demontaj", "DIGER": "Diğer"}
POLICIES = {"ANA_FIYAT_ONERILIR": "Ana poz fiyatı önerilir (onayla uygulanır)",
            "AYRI_FIYAT_GEREKIR": "Ayrı doğrulanmış fiyat ya da kullanıcı bedeli gerekir"}
DECISIONS = ("ONAY", "DUZENLENEREK_ONAY", "RED")


@dataclass(frozen=True)
class Rule:
    id: int
    suffix: str
    meaning: str
    kind: str
    price_policy: str
    enabled: bool
    authority_id: int | None
    project_id: int | None
    is_default: bool
    notes: str | None
    authority_name: str | None = None
    project_name: str | None = None

    @property
    def scope_label(self) -> str:
        if self.project_id:
            return f"Proje: {self.project_name or self.project_id}"
        return f"Kurum: {self.authority_name}" if self.authority_id else "Genel"


@dataclass(frozen=True)
class SubcodeCandidate:
    price_id: int
    code: str
    description: str
    unit: str
    price: Decimal
    montage_price: Decimal | None
    authority_id: int
    authority: str
    book_id: int
    book_label: str
    period: str | None
    valid_from: str | None
    price_kind: str
    verification: str
    source_ref: str | None
    page: int | None
    printed_page: str | None

    @property
    def period_text(self) -> str:
        return f"{text_or_unspecified(self.period)} · geçerlilik {date_tr(self.valid_from)}"


@dataclass(frozen=True)
class SubcodeProposal:
    item_id: int
    original_code: str
    base_code: str
    suffix: str
    exact: tuple[SubcodeCandidate, ...]        # ekli kodun kendisi resmî kayıtta varsa (kural uygulanmaz)
    rule: Rule | None                          # None: ek için kural yok ya da kapalı
    base: tuple[SubcodeCandidate, ...]         # ana poz kayıtları (öncelik sırasıyla)
    separate: tuple[SubcodeCandidate, ...]     # AYRI_FIYAT_GEREKIR için doğrulanmış ayrı (ör. DEMONTAJ) fiyatlar
    conflicts: tuple[str, ...]                 # kuralın resmî kaynakla çelişebileceği durumlar

    @property
    def meaning(self) -> str:
        return self.rule.meaning if self.rule else "Kural tanımlı değil"

    @property
    def needs_separate_price(self) -> bool:
        return self.rule is not None and self.rule.price_policy == "AYRI_FIYAT_GEREKIR"

    def suggested(self) -> tuple[SubcodeCandidate | None, Decimal | None]:
        """Önerilen kayıt ve uygulanacak fiyat; dayanak yoksa fiyat None (kullanıcı girmeli)."""
        if self.rule is None:
            return None, None
        if self.needs_separate_price:
            return (self.separate[0], self.separate[0].price) if self.separate else (
                self.base[0] if self.base else None, None)
        return (self.base[0], self.base[0].price) if self.base else (None, None)


def _rule(r: sqlite3.Row) -> Rule:
    return Rule(r["id"], r["suffix"], r["meaning"], r["kind"], r["price_policy"], bool(r["enabled"]), r["authority_id"],
                r["project_id"], bool(r["is_default"]), r["notes"], r["authority_name"], r["project_name"])


_RULE_SELECT = ("SELECT r.*, a.name AS authority_name, p.name AS project_name FROM subcode_rules r"
                " LEFT JOIN authorities a ON a.id = r.authority_id LEFT JOIN projects p ON p.id = r.project_id")


class SubcodeService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- kurallar ------------------------------------------------------------------------------------
    def rules(self) -> list[Rule]:
        return [_rule(r) for r in self.conn.execute(
            _RULE_SELECT + " ORDER BY r.suffix, r.project_id IS NOT NULL, r.authority_id IS NOT NULL, r.id")]

    def get_rule(self, rule_id: int) -> Rule:
        r = self.conn.execute(_RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone()
        if r is None:
            raise NotFoundError("Alt poz kuralı bulunamadı.")
        return _rule(r)

    @staticmethod
    def _check(suffix: str, meaning: str, kind: str, policy: str) -> tuple[str, str]:
        suffix = (suffix or "").strip().upper()
        if not (2 <= len(suffix) <= 6 and suffix[0] in "/-" and suffix[1:].isalnum()):
            raise ValidationError("Ek '/' ya da '-' ile başlamalı ve harf/rakam içermeli (ör. /A, -D).")
        meaning = clean(meaning) or ""
        if not meaning:
            raise ValidationError("Kuralın anlamı boş olamaz.")
        if kind not in KINDS or policy not in POLICIES:
            raise ValidationError("Kural türü ya da fiyat politikası geçersiz.")
        if kind == "DEMONTAJ" and policy != "AYRI_FIYAT_GEREKIR":
            raise ValidationError("Demontaj bedeli montaj fiyatından türetilemez; politika 'ayrı fiyat gerekir' olmalı.")
        return suffix, meaning

    def save_rule(self, suffix: str, meaning: str, kind: str, price_policy: str, enabled: bool = True,
                  authority_id: int | None = None, project_id: int | None = None, notes: str | None = None,
                  rule_id: int | None = None) -> int:
        suffix, meaning = self._check(suffix, meaning, kind, price_policy)
        clash = self.conn.execute(
            "SELECT id FROM subcode_rules WHERE suffix = ? AND IFNULL(authority_id, 0) = ? AND IFNULL(project_id, 0) = ?"
            " AND id IS NOT ?", (suffix, authority_id or 0, project_id or 0, rule_id)).fetchone()
        if clash:
            raise ConflictError(f"'{suffix}' için bu kapsamda zaten bir kural var.")
        vals = (suffix, meaning, kind, price_policy, int(enabled), authority_id, project_id, clean(notes))
        with transaction(self.conn):
            if rule_id is None:
                rule_id = self.conn.execute(
                    "INSERT INTO subcode_rules(suffix, meaning, kind, price_policy, enabled, authority_id, project_id,"
                    " notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", vals).lastrowid
                audit.log(self.conn, "subcode_rule", rule_id, "EKLE", new={"suffix": suffix, "meaning": meaning})
            else:
                old = self.get_rule(rule_id)
                self.conn.execute(
                    "UPDATE subcode_rules SET suffix = ?, meaning = ?, kind = ?, price_policy = ?, enabled = ?,"
                    " authority_id = ?, project_id = ?, notes = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE id = ?", (*vals, rule_id))
                audit.log(self.conn, "subcode_rule", rule_id, "GUNCELLE", old={"meaning": old.meaning,
                          "enabled": old.enabled}, new={"meaning": meaning, "enabled": enabled})
        return rule_id

    def set_enabled(self, rule_id: int, enabled: bool) -> None:
        r = self.get_rule(rule_id)
        self.save_rule(r.suffix, r.meaning, r.kind, r.price_policy, enabled, r.authority_id, r.project_id, r.notes,
                       rule_id)

    def delete_rule(self, rule_id: int) -> None:
        r = self.get_rule(rule_id)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM subcode_rules WHERE id = ?", (rule_id,))
            audit.log(self.conn, "subcode_rule", rule_id, "SIL", old={"suffix": r.suffix, "meaning": r.meaning})

    def resolve(self, suffix: str, project_id: int | None, authority_id: int | None) -> Rule | None:
        """En özel kural: proje (+kurum) > proje > kurum > genel. Kapalı kural None döndürür (öneri yok)."""
        rows = [_rule(r) for r in self.conn.execute(_RULE_SELECT + " WHERE r.suffix = ?", (suffix.upper(),))]
        ranked = sorted(
            (r for r in rows if (r.project_id in (None, project_id)) and (r.authority_id in (None, authority_id))),
            key=lambda r: (r.project_id is None, r.authority_id is None))
        if not ranked:
            return None
        return ranked[0] if ranked[0].enabled else None

    # --- öneri ---------------------------------------------------------------------------------------
    def _rows(self, code_norm: str, book_ids: list[int], kind: str | None = None) -> list[SubcodeCandidate]:
        sql = ("SELECT pp.id, p.code, pp.description, pp.unit, pp.unit_price, pp.montage_price, pp.source_page,"
               " pp.printed_page, a.id AS authority_id, COALESCE(a.short_name, a.name) AS auth, pb.id AS book_id,"
               " pb.title, pb.year, pb.edition, pb.period, pb.valid_from, pb.price_kind, pb.verification, pb.source_ref,"
               " pb.status"
               " FROM positions p JOIN position_prices pp ON pp.position_id = p.id"
               " JOIN price_books pb ON pb.id = pp.price_book_id JOIN authorities a ON a.id = pb.authority_id"
               " WHERE p.code_norm = ? AND pb.hidden = 0"
               f" AND pb.verification NOT IN ({', '.join('?' * len(UNVERIFIED))})")
        params: list = [code_norm, *UNVERIFIED]
        if kind:
            sql += " AND pb.price_kind = ?"
            params.append(kind)
        # Eşleştirmeyle aynı kural: keşfin kaynak önceliğindeki kitap (TASLAK olsa da) + diğer AKTIF kitaplar;
        # öncelikteki kitaplar önce gelir.
        order = {b: i for i, b in enumerate(book_ids)}
        rows = [r for r in self.conn.execute(sql, params) if r["status"] == "AKTIF" or r["book_id"] in order]
        rows.sort(key=lambda r: (order.get(r["book_id"], 999), -r["year"], r["valid_from"] or ""))
        return [SubcodeCandidate(
            r["id"], r["code"], r["description"], r["unit"], from_db_text(r["unit_price"]),
            None if r["montage_price"] is None else from_db_text(r["montage_price"]), r["authority_id"], r["auth"],
            r["book_id"], f"{r['title']} {r['year']}" + (f" {r['edition']}" if r["edition"] else ""), r["period"],
            r["valid_from"], r["price_kind"], r["verification"], r["source_ref"], r["source_page"], r["printed_page"])
            for r in rows]

    def propose(self, item_id: int, book_ids: list[int] | None = None) -> SubcodeProposal | None:
        """Ekli kodlu kalem için öneri; kod ekli değilse None. `book_ids`: aranacak kitaplar (varsayılan: keşfin
        kaynak önceliği; o da boşsa tüm aktif kitaplar)."""
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        parts = split_suffix(it.code)
        if not parts:
            return None
        base_code, suffix = parts
        est = projects.get_estimate(self.conn, it.estimate_id)
        books = list(book_ids if book_ids is not None else (est.source_priority if est else []))
        exact = self._rows(normalize_code(it.code), books)
        base = self._rows(normalize_code(base_code), books)
        authority = base[0].authority_id if base else None
        rule = self.resolve(suffix, est.project_id if est else None, authority)
        separate: list[SubcodeCandidate] = []
        if rule and rule.price_policy == "AYRI_FIYAT_GEREKIR":
            separate = [c for c in self._rows(normalize_code(base_code), books, "DEMONTAJ")
                        if c.verification == "DOGRULANDI"]
        conflicts = self._conflicts(it, base_code, suffix, rule, books, exact)
        return SubcodeProposal(item_id, it.code, base_code, suffix, tuple(exact), rule, tuple(base), tuple(separate),
                               tuple(conflicts))

    def _conflicts(self, it, base_code: str, suffix: str, rule: Rule | None, books: list[int], exact) -> list[str]:
        out = []
        if rule is None:
            out.append(f"'{suffix}' için açık bir alt poz kuralı yok; öneri yapılmaz (Ayarlar > Alt Poz Kuralları).")
            return out
        if exact:
            out.append(f"'{it.code}' kodu kaynakta resmî olarak tanımlı; kural yerine resmî kayıt kullanılmalı.")
        if rule.kind in ("VIDALI", "KAYNAKLI"):
            family = base_code[:6]
            ids = books or [r[0] for r in self.conn.execute("SELECT id FROM price_books WHERE status = 'AKTIF'")]
            for rate in rate_positions.for_books(self.conn, ids):
                if rate.code.startswith(family) and "montaj malzeme" in rate.description.lower():
                    out.append(f"Kaynak kitapta {rate.code} '{rate.description[:70]}' (%{rate.rate}) oran pozu var; "
                               f"kuralınız '{rule.meaning}' bu ekin resmî anlamıyla çelişebilir.")
                    break
        if rule.is_default:
            out.append(f"'{suffix}' = '{rule.meaning}' program varsayılanıdır; kurumun resmî tarifi değildir.")
        return out

    # --- karar ---------------------------------------------------------------------------------------
    def decide(self, item_id: int, decision: str, price_id: int | None = None, applied_price: Decimal | None = None,
               note: str | None = None, book_ids: list[int] | None = None) -> int:
        """Kullanıcı kararı. ONAY: seçilen kayıt fiyatıyla eşleştirir; DUZENLENEREK_ONAY: kayıt + kullanıcı bedeli;
        RED: kalem eşleşmemiş kalır. Döndürür: karar kaydı id."""
        if decision not in DECISIONS:
            raise ValidationError("Karar geçersiz.")
        it = estimate_items.get_item(self.conn, item_id)
        if it is None:
            raise NotFoundError("Keşif kalemi bulunamadı.")
        est = projects.get_estimate(self.conn, it.estimate_id)
        if est is None or est.locked:
            raise ConflictError("Keşif kilitli; eşleştirme değiştirilemez.")
        prop = self.propose(item_id, book_ids)
        if prop is None:
            raise ValidationError("Kalem kodu ekli bir alt poz değil.")
        chosen = None
        if decision != "RED":
            if prop.rule is None and not prop.exact:
                raise ConflictError("Bu ek için açık bir kural yok; öneri uygulanamaz. 'Başka Poz Seç' kullanın.")
            pool = {c.price_id: c for c in (*prop.exact, *prop.base, *prop.separate)}
            if price_id is None:
                chosen, suggested_price = prop.suggested()
                price_id = chosen.price_id if chosen else None
            if price_id not in pool:
                raise ValidationError("Seçilen kayıt öneri listesinde değil.")
            chosen = pool[price_id]
            separate_ok = chosen in prop.separate or chosen in prop.exact
            if decision == "ONAY":
                if prop.needs_separate_price and not separate_ok:
                    raise ConflictError("Demontaj bedeli montaj fiyatından türetilmez: doğrulanmış ayrı demontaj fiyatı "
                                        "yok. Fiyatı düzenleyerek (dayanağını yazarak) onaylayın.")
                applied_price = chosen.price
            else:
                if applied_price is None:
                    raise ValidationError("Düzenlenerek onayda uygulanacak birim fiyat girilmelidir.")
                applied_price = validate_price(applied_price)
                if prop.needs_separate_price and not separate_ok and not clean(note):
                    raise ValidationError("Demontaj bedelinin dayanağını (açıklama) yazın.")
        with transaction(self.conn):
            if chosen is not None:
                estimate_items.set_match(self.conn, item_id, "ONAYLANDI", chosen.price_id, None, False, "KULLANICI")
                updated = estimate_items.get_item(self.conn, item_id)
                if updated.unit_mismatch:   # birim farkı onay penceresinde gösterildi; kullanıcı onayı kabul sayılır
                    estimate_items.set_match(self.conn, item_id, "ONAYLANDI", chosen.price_id, None, True, "KULLANICI")
                override = None if decision == "ONAY" and applied_price == chosen.price else applied_price
                estimate_items.set_override(self.conn, item_id, override)
            from yapimmaliyet.infra import authz
            session = authz.current()
            did = self.conn.execute(
                "INSERT INTO subcode_decisions(estimate_item_id, original_code, base_code, suffix, rule_meaning, decision,"
                " position_price_id, authority_name, book_label, period, valid_from, source_price, applied_price, note,"
                " user_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, prop.original_code, prop.base_code, prop.suffix, prop.rule.meaning if prop.rule else None,
                 decision, chosen.price_id if chosen else None, chosen.authority if chosen else None,
                 chosen.book_label if chosen else None, chosen.period if chosen else None,
                 chosen.valid_from if chosen else None, to_db_text(chosen.price, 4) if chosen else None,
                 None if applied_price is None else to_db_text(applied_price, 4), clean(note),
                 getattr(session, "username", None) if session else None)).lastrowid
            audit.log(self.conn, "estimate_item", item_id, "GUNCELLE",
                      new={"subcode": prop.original_code, "decision": decision, "price_id": price_id,
                           "applied_price": applied_price}, note="Alt poz önerisi kararı")
        return did

    def decisions(self, item_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM subcode_decisions WHERE estimate_item_id = ? ORDER BY id DESC",
                                 (item_id,)).fetchall()
