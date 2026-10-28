"""Tutarın Türkçe yazıyla gösterimi (teklif cetveli ve teklif mektubu taslakları için): 1234,50 → "Bin İki Yüz
Otuz Dört Türk Lirası Elli Kuruş". "Bir yüz" ve "bir bin" yerine "yüz" ve "bin" yazılır; "bir milyon" korunur."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ONES = ["", "Bir", "İki", "Üç", "Dört", "Beş", "Altı", "Yedi", "Sekiz", "Dokuz"]
_TENS = ["", "On", "Yirmi", "Otuz", "Kırk", "Elli", "Altmış", "Yetmiş", "Seksen", "Doksan"]
_SCALES = ["", "Bin", "Milyon", "Milyar", "Trilyon"]


def _three(n: int) -> list[str]:
    h, rest = divmod(n, 100)
    t, o = divmod(rest, 10)
    words = []
    if h:
        words += ([] if h == 1 else [_ONES[h]]) + ["Yüz"]
    if t:
        words.append(_TENS[t])
    if o:
        words.append(_ONES[o])
    return words


def integer_words(n: int) -> str:
    if n < 0:
        raise ValueError("Negatif tutar yazıyla yazılmaz.")
    if n == 0:
        return "Sıfır"
    groups = []
    while n:
        n, g = divmod(n, 1000)
        groups.append(g)
    if len(groups) > len(_SCALES):
        raise ValueError("Tutar çok büyük.")
    words: list[str] = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if not g:
            continue
        if i == 1 and g == 1:          # "bin", "bir bin" değil
            words.append("Bin")
            continue
        words += _three(g) + ([_SCALES[i]] if i else [])
    return " ".join(words)


def amount_in_words(amount: Decimal, currency: str = "Türk Lirası", sub: str = "Kuruş") -> str:
    q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q < 0:
        raise ValueError("Negatif tutar yazıyla yazılmaz.")
    lira, kurus = divmod(int(q * 100), 100)
    text = f"{integer_words(lira)} {currency}"
    return text + (f" {integer_words(kurus)} {sub}" if kurus else "")
