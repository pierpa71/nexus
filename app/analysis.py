"""Analisi completa di un documento ("map-reduce").

Per le richieste che riguardano TUTTO il documento (es. "tabella con tutti gli articoli", "elenca tutte le
scadenze", "riassumi capitolo per capitolo") il RAG non basta: al modello arrivano solo i passaggi più
pertinenti. Qui il documento viene passato al modello a blocchi che stanno nella sua finestra di contesto;
da ogni blocco il modello estrae ciò che serve alla richiesta, poi i risultati parziali vengono uniti in
un'unica risposta (dal modello, se ci stanno nel contesto; altrimenti concatenandoli in ordine)."""
from __future__ import annotations

import logging
import re

from openai import BadRequestError

from . import kbdb

log = logging.getLogger("nexus.analysis")

NOTHING = "NIENTE"
CHARS_PER_TOKEN = 3.2   # stima prudente per testi italiani


def tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def make_batches(chunks: list[dict], budget_tokens: int) -> list[list[dict]]:
    batches, cur, size = [], [], 0
    for c in chunks:
        t = tokens(c["content"]) + 8
        if cur and size + t > budget_tokens:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += t
    if cur:
        batches.append(cur)
    return batches


def _pages(batch) -> str:
    pages = [c["page"] for c in batch if c["page"]]
    if not pages:
        return ""
    lo, hi = min(pages), max(pages)
    return f"pag. {lo}" if lo == hi else f"pag. {lo}–{hi}"


def _table_header(text: str) -> list[str] | None:
    """Intestazione (riga dei titoli + separatore) della prima tabella Markdown del testo."""
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("|") and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            return [lines[i].rstrip(), lines[i + 1].rstrip()]
    return None


def _norm(line: str) -> str:
    return re.sub(r"[\s|:-]+", " ", line).strip().lower()


_ROMAN = re.compile(r"^[ivxlcdm]+$")


def _row_key(line: str) -> str | None:
    """Identificativo della riga (prima cella) se è un numero o un codice breve: "Art. 81." -> "81"."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 2:
        return None
    key = re.sub(r"^(art(icolo)?|n(um(ero)?)?|cap(itolo)?|par(agrafo)?|§)\.?\s*", "", cells[0].lower().strip("*_ ")).strip(" .:")
    if not key or len(key) > 12:
        return None
    if re.search(r"\d", key) or _ROMAN.match(key):
        return key
    return None


def _merge_rows(lines: list[str]) -> list[str]:
    """Fonde le righe di tabella con lo stesso identificativo (es. un articolo spezzato in più righe,
    o ripetuto da due blocchi confinanti) unendo il testo delle altre colonne, senza ripetizioni."""
    out, index = [], {}
    for line in lines:
        key = _row_key(line) if line.strip().startswith("|") and not re.match(r"^\s*\|?\s*:?-{2,}", line) else None
        if key is None or key not in index:
            if key is not None:
                index[key] = len(out)
            out.append(line)
            continue
        old = [c.strip() for c in out[index[key]].strip().strip("|").split("|")]
        new = [c.strip() for c in line.strip().strip("|").split("|")]
        for i in range(1, min(len(old), len(new))):
            if new[i] and _norm(new[i]) not in _norm(old[i]):
                old[i] = f"{old[i]} {new[i]}".strip()
        out[index[key]] = "| " + " | ".join(old) + " |"
    return out


def concatenate(partials: list[str]) -> str:
    """Unisce i risultati parziali in ordine; se sono tabelle con la stessa intestazione, ne fa una sola
    e fonde le righe che si riferiscono allo stesso elemento."""
    header = None
    out = []
    for p in partials:
        lines = p.strip().splitlines()
        h = _table_header(p)
        if h and header is None:
            header = h
        elif h and header and _norm(h[0]) == _norm(header[0]):
            idx = next(i for i, l in enumerate(lines) if l.rstrip() == h[0])
            lines = lines[:idx] + lines[idx + 2:]
        out.append("\n".join(lines).strip())
    if not header:
        return "\n\n".join(out)
    lines = [l for l in "\n".join(out).splitlines() if l.strip()]
    return "\n".join(_merge_rows(lines))


MAP_PROMPT = """Stai analizzando il documento «{name}» un blocco alla volta, perché è troppo lungo per essere letto \
in una volta sola. Questo è il blocco {i} di {n} ({pages}).

Richiesta dell'utente su tutto il documento:
«{request}»

Estrai da QUESTO blocco tutto ciò che serve per soddisfare la richiesta, in modo completo: non saltare elementi \
e non riassumere ciò che va elencato. Rispondi solo con il contenuto estratto, senza introduzioni né commenti, \
nella stessa forma che avrà la risposta finale (per esempio le righe di una tabella Markdown, o un elenco puntato).
Una sola riga per ogni elemento (es. un articolo con più commi = una riga), con il numero o il titolo copiato \
esattamente dal testo accanto al suo contenuto. Ignora gli indici e i sommari: usa il testo vero e proprio.\
{header_hint}
Usa solo il testo del blocco. Se il blocco non contiene nulla di utile per la richiesta rispondi esattamente: {nothing}

<blocco>
{text}
</blocco>"""

REDUCE_PROMPT = """Ho analizzato tutto il documento «{name}» in {n} blocchi, nell'ordine. Questi sono i risultati \
estratti da ciascun blocco per la richiesta dell'utente:

{parts}

Richiesta dell'utente: «{request}»

Scrivi la risposta finale unendo i risultati: mantieni l'ordine del documento, elimina i doppioni (i blocchi \
confinanti possono ripetere lo stesso elemento), non omettere nessun elemento e non aggiungere informazioni \
che non sono nei risultati. Se la richiesta chiede una tabella, produci un'unica tabella."""


async def _complete(client, model, prompt, max_tokens, temperature, extra):
    kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature,
              "max_tokens": max_tokens, **extra}
    try:
        res = await client.chat.completions.create(**kwargs)
    except BadRequestError:
        if not extra:
            raise
        for k in list(extra):
            kwargs.pop(k, None)
        res = await client.chat.completions.create(**kwargs)
    msg = res.choices[0].message
    text = msg.content or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, res.choices[0].finish_reason


async def run(request: str, doc_id: int, client, model: str, ctx: int, think_off: dict, final_kwargs: dict):
    """Generatore di eventi: ("analysis", {...}) per l'avanzamento, ("notice", {...}), ("final_prompt", str)
    quando il modello deve scrivere la risposta finale in streaming, oppure ("text", str) con la risposta già
    pronta (concatenazione)."""
    doc = await kbdb.get_document(doc_id)
    if not doc or doc["status"] != "ready":
        raise ValueError("Il documento scelto per l'analisi completa non è disponibile o non è ancora indicizzato.")
    chunks = await kbdb.fetch("SELECT chunk_index, page, content FROM chunks WHERE document_id=%s "
                              "ORDER BY chunk_index", (doc_id,))
    name = doc["filename"]
    ctx = max(2048, int(ctx or 8192))
    out_tokens = max(512, int(ctx * 0.35))
    prompt_overhead = tokens(MAP_PROMPT) + tokens(request) + 200
    # blocchi piu' piccoli del massimo: i modelli piccoli estraggono in modo piu' accurato e completo
    budget = max(400, min(ctx - out_tokens - prompt_overhead - 256, int(ctx * 0.33)))
    batches = make_batches(chunks, budget)
    n = len(batches)
    yield "analysis", {"phase": "map", "done": 0, "total": n, "doc": name, "doc_id": doc_id}

    partials, truncated = [], 0
    state = {"header": None}

    async def extract(batch, i, depth=0) -> list[tuple[str, bool]]:
        """Estrae da un blocco: lista di (testo, troncato). Se la risposta viene tagliata, divide il blocco
        a meta' e riprova sulle due parti."""
        header = state["header"]
        hint = (f"\nSe produci una tabella usa esattamente questa intestazione, senza ripeterne il titolo:\n"
                f"{header[0]}\n{header[1]}") if header else ""
        prompt = MAP_PROMPT.format(name=name, i=i, n=n, pages=_pages(batch) or "senza numeri di pagina",
                                   request=request, header_hint=hint, nothing=NOTHING,
                                   text="\n\n".join(c["content"] for c in batch))
        text, finish = await _complete(client, model, prompt, out_tokens, 0.1, think_off)
        if finish == "length" and len(batch) > 1 and depth < 3:
            mid = len(batch) // 2
            return await extract(batch[:mid], i, depth + 1) + await extract(batch[mid:], i, depth + 1)
        clean = text.strip().strip("`").strip()
        if not clean or clean.upper().rstrip(".") == NOTHING:
            return [("", finish == "length")]
        state["header"] = state["header"] or _table_header(text)
        return [(text.strip(), finish == "length")]

    for i, batch in enumerate(batches, start=1):
        for text, cut in await extract(batch, i):
            truncated += cut
            if text:
                partials.append(text)
        yield "analysis", {"phase": "map", "done": i, "total": n, "doc": name, "doc_id": doc_id,
                           "pages": _pages(batch), "found": len(partials)}

    if truncated:
        yield "notice", {"text": f"In {truncated} blocchi su {n} il risultato è stato tagliato perché superava "
                                 "lo spazio disponibile: alcuni elementi potrebbero mancare. Aumenta il contesto "
                                 "di Ollama (OLLAMA_CONTEXT_LENGTH) per blocchi più ampi."}
    if not partials:
        yield "text", f"Ho letto tutto «{name}» ({n} blocchi) ma non ho trovato nulla che corrisponda alla richiesta."
        return

    joined = "\n\n".join(f"### Blocco {i + 1}\n{p}" for i, p in enumerate(partials))
    reduce_prompt = REDUCE_PROMPT.format(name=name, n=n, parts=joined, request=request)
    need = tokens(reduce_prompt)
    # la risposta finale deve contenere tutti gli elementi: servono spazio in ingresso E in uscita
    if need * 2 + 300 <= ctx:
        yield "analysis", {"phase": "reduce", "done": n, "total": n, "doc": name, "doc_id": doc_id}
        yield "final_prompt", {"prompt": reduce_prompt, "max_tokens": max(512, ctx - need - 200)}
        return
    yield "analysis", {"phase": "concat", "done": n, "total": n, "doc": name, "doc_id": doc_id}
    yield "notice", {"text": "I risultati sono troppo lunghi per essere rielaborati in un unico passaggio dal "
                             "modello: li ho uniti nell'ordine del documento. Possono esserci piccole ripetizioni "
                             "tra blocchi confinanti."}
    yield "text", concatenate(partials)
