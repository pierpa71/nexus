"""Riconoscimento della lingua (senza dipendenze esterne) basato sulle parole funzionali più frequenti.

Restituisce il nome della configurazione di text search di PostgreSQL ('italian', 'english', ...),
usata per stemming e stopword nella ricerca lessicale della knowledge base."""
from __future__ import annotations

import re
from collections import Counter

STOPWORDS = {
    "italian": "il lo la i gli le un una di del della dei delle che non per con sono è da al alla nel nella "
               "come anche ma più questo questa sono essere ha hanno su tra dal dalla degli ed se gli cui",
    "english": "the of and to in is that for it with as was on be by this are or from at an which have "
               "not has were but their they will can its been would should shall any such these",
    "french": "le la les des du de et est un une que qui dans pour pas sur au aux avec ce cette sont par "
              "plus ne il elle nous vous leur ont été être",
    "german": "der die das und ist nicht ein eine zu den von mit sich des auf für im dem auch es an werden "
              "aus er hat dass sie nach wird bei einer um",
    "spanish": "el la los las de del que y en un una es por con para no se su al lo como más pero sus le "
               "ya o este esta son entre cuando muy",
}
_WORDS = {lang: set(words.split()) for lang, words in STOPWORDS.items()}
LABELS = {"italian": "IT", "english": "EN", "french": "FR", "german": "DE", "spanish": "ES", "simple": "—"}
AVAILABLE = list(STOPWORDS)


def enabled(languages, fallback: str = "italian") -> list[str]:
    langs = [lg for lg in (languages or []) if lg in _WORDS]
    return langs or [fallback if fallback in _WORDS else "italian"]


def detect(text: str, languages, fallback: str = "italian") -> str:
    """Lingua prevalente del testo tra quelle abilitate; `fallback` se il testo è troppo breve o ambiguo."""
    tokens = re.findall(r"[a-zà-ÿ']+", text.lower())
    if len(tokens) < 8:
        return fallback
    counts = Counter()
    langs = enabled(languages, fallback)
    for tok in tokens:
        for lang in langs:
            if tok in _WORDS[lang]:
                counts[lang] += 1
    if not counts:
        return fallback
    (best, n1), *rest = counts.most_common(2) + [(None, 0)]
    n2 = rest[0][1]
    # serve un margine netto, altrimenti il testo è misto o troppo tecnico
    if n1 < 3 or n1 < 1.5 * n2:
        return fallback
    return best


def normalize(value: str | None, languages) -> str | None:
    """Accetta 'auto', 'it', 'en', 'italian', 'english', ... e restituisce il nome PostgreSQL (o None = auto)."""
    if not value or value.lower() in ("auto", "automatica"):
        return None
    v = value.lower()
    for lang, label in LABELS.items():
        if v in (lang, label.lower()):
            if lang != "simple" and lang not in enabled(languages):
                raise ValueError(f"Lingua '{value}' non abilitata nelle impostazioni della knowledge base")
            return lang
    raise ValueError(f"Lingua non riconosciuta: {value}")
