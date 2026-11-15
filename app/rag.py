"""Knowledge base: indicizzazione (estrazione testo + OCR, chunking per pagina, lingua per passaggio,
embedding) e ricerca ibrida — similarità vettoriale + full-text con stemming — fusa con Reciprocal Rank Fusion.

I documenti e i passaggi stanno in PostgreSQL + pgvector (kbdb.py); gli originali in DATA_DIR/uploads."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os

import numpy as np

from . import db, kbdb, language, parsing
from .llm import embed_client

log = logging.getLogger("nexus.rag")

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------- Originali ----------------
def file_path(doc_id: int) -> str:
    return os.path.join(UPLOAD_DIR, f"kb-{doc_id}.bin")


def save_original(doc_id: int, data: bytes):
    with open(file_path(doc_id), "wb") as f:
        f.write(data)


def read_original(doc_id: int) -> bytes | None:
    try:
        with open(file_path(doc_id), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def delete_original(doc_id: int):
    try:
        os.remove(file_path(doc_id))
    except FileNotFoundError:
        pass


# ---------------- Impostazioni ----------------
def kb_languages(s) -> list[str]:
    langs = s.get("kb_languages") or ["italian", "english"]
    if isinstance(langs, str):
        langs = [x.strip() for x in langs.split(",")]
    return language.enabled(langs, s.get("kb_fallback_language") or "italian")


def fallback_language(s) -> str:
    fb = s.get("kb_fallback_language") or "italian"
    return fb if fb in language.AVAILABLE else kb_languages(s)[0]


def extract_options(s) -> parsing.ExtractOptions:
    return parsing.ExtractOptions(kb_languages(s), s.get("ocr_mode") or "auto", int(s.get("ocr_workers") or 2))


def explain_embed_error(e: Exception, settings) -> str:
    """Trasforma gli errori più comuni in messaggi con la soluzione."""
    msg = f"{type(e).__name__}: {e}"
    low = str(e).lower()
    model = settings["embed_model"]
    if "not found" in low and "model" in low:
        return (f"Il modello di embedding '{model}' non è installato sull'endpoint. "
                f"Con Ollama esegui: ollama pull {model} — poi premi Riprova.")
    if "does not support embed" in low or "not an embedding" in low:
        return f"Il modello '{model}' non supporta gli embedding: scegline uno dedicato (es. bge-m3, nomic-embed-text)."
    if "connect" in low or "connection" in low:
        return f"Endpoint di embedding non raggiungibile ({settings.get('embed_base_url') or settings['llm_base_url']}): {msg}"
    return msg


# ---------------- Embedding ----------------
async def embed(texts, settings):
    client = embed_client(settings)
    out = []
    for i in range(0, len(texts), 32):
        batch = texts[i:i + 32]
        res = await client.embeddings.create(model=settings["embed_model"], input=batch)
        out.extend(d.embedding for d in sorted(res.data, key=lambda d: d.index))
    arr = np.array(out, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return arr / norms


def _doc_text(filename: str, text: str) -> str:
    # il nome del file aiuta il retrieval: lo anteponiamo al testo da vettorializzare
    return f"{filename}\n{text}"


async def adopt_embedding_model(s, dim: int | None = None) -> bool:
    """Allinea l'archivio al modello di embedding delle impostazioni (misurandone la dimensione).
    Restituisce True se i vettori esistenti sono stati invalidati e vanno ricalcolati."""
    if dim is None:
        dim = int((await embed(["Nexus: verifica del modello di embedding."], s)).shape[1])
    changed = await kbdb.switch_embedding(s["embed_model"], dim)
    if changed:
        start_reembed()
    return changed


async def _signature(s, sample_dim: int):
    """Firma del modello corrente; alla prima indicizzazione (o dopo un cambio) adotta il modello."""
    meta = await kbdb.embedding_meta()
    if not meta or meta.get("model") != s["embed_model"] or meta.get("dim") != sample_dim:
        await adopt_embedding_model(s, sample_dim)
    return {"model": s["embed_model"], "dim": sample_dim}


# ---------------- Indicizzazione ----------------
async def register_upload(filename: str, data: bytes, lang: str | None = None) -> dict:
    """Registra un file caricato. I duplicati (stesso contenuto) non vengono indicizzati di nuovo;
    un duplicato di un documento in errore viene riprovato."""
    s = db.get_settings()
    forced = language.normalize(lang, kb_languages(s))
    sha = hashlib.sha256(data).hexdigest()
    existing = await kbdb.find_by_hash(sha)
    if existing:
        if existing["status"] == "error":
            save_original(existing["id"], data)
            await kbdb.execute("UPDATE documents SET has_original=true, forced_lang=%s WHERE id=%s",
                               (forced, existing["id"]))
            await kbdb.set_status(existing["id"], "processing")
            return {"id": existing["id"], "name": filename, "status": "retry"}
        return {"id": existing["id"], "name": filename, "status": "duplicate", "existing": existing["filename"]}
    doc_id = await kbdb.create_document(filename, mimetypes.guess_type(filename)[0], sha, len(data), forced_lang=forced)
    save_original(doc_id, data)
    return {"id": doc_id, "name": filename, "status": "processing"}


_ingesting: set[int] = set()


async def ingest(doc_id: int):
    """Estrae, suddivide, riconosce la lingua e calcola gli embedding di un documento già registrato."""
    if doc_id in _ingesting:  # già in corso (es. doppio clic su Riprova)
        return
    _ingesting.add(doc_id)
    try:
        await _ingest(doc_id)
    finally:
        _ingesting.discard(doc_id)


async def _ingest(doc_id: int):
    s = db.get_settings()
    doc = await kbdb.get_document(doc_id)
    if not doc:
        return
    await kbdb.set_status(doc_id, "processing")
    try:
        data = read_original(doc_id)
        if data is None:
            raise ValueError("File originale non disponibile: ricaricalo.")
        filename = doc["filename"]
        try:
            # estrazione e OCR sono lenti e sincroni: fuori dal ciclo degli eventi
            pages, info = await asyncio.to_thread(parsing.extract, filename, data, extract_options(s))
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Impossibile leggere il file: {type(e).__name__}: {e}") from e
        chunks = parsing.chunk(pages, s["chunk_size"], s["chunk_overlap"])
        if not chunks:
            raise ValueError("Nessun testo utile trovato nel documento.")
        langs_on = kb_languages(s)
        fb = fallback_language(s)
        # lingua del documento e di ogni passaggio: i documenti misti sono frequenti
        # (es. contratto in italiano con allegato tecnico in inglese)
        if doc["forced_lang"]:
            doc_lang, langs = doc["forced_lang"], [doc["forced_lang"]] * len(chunks)
        else:
            doc_lang = language.detect(" ".join(t for _, t in chunks[:50]), langs_on, fb)
            langs = [language.detect(t, langs_on, doc_lang) for _, t in chunks]
        try:
            vectors = await embed([_doc_text(filename, t) for _, t in chunks], s)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(explain_embed_error(e, s)) from e
        signature = await _signature(s, int(vectors.shape[1]))
        num_pages = max((p for p, _ in pages if p), default=None)
        stale = await kbdb.store_chunks(doc_id, chunks, langs, vectors, signature, num_pages, doc_lang,
                                        {"ocr_pages": info.get("ocr_pages", [])})
        if stale:  # il modello di embedding è cambiato durante l'indicizzazione
            start_reembed()
    except Exception as e:  # noqa: BLE001
        err = str(e) if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"
        log.warning("Indicizzazione del documento %s fallita: %s", doc_id, err)
        await kbdb.set_status(doc_id, "error", err)


async def import_text_document(filename: str, pages: list[tuple[int | None, str]], *, sha: str | None = None,
                               langs: list[str] | None = None, language_: str | None = None,
                               ocr_pages=None, source: str = "", size: int = 0, num_pages=None,
                               original: bytes | None = None, background: bool = True) -> dict:
    """Importa un documento di cui si hanno già i passaggi (es. da Tabularium o dalla versione SQLite
    di Nexus): niente estrazione né OCR; gli embedding vengono calcolati dal processo di ricalcolo."""
    s = db.get_settings()
    pages = [(p, t) for p, t in pages if t and t.strip()]
    if not pages:
        return {"name": filename, "status": "empty"}
    sha = sha or hashlib.sha256("\n".join(t for _, t in pages).encode()).hexdigest()
    existing = await kbdb.find_by_hash(sha)
    if existing:
        return {"id": existing["id"], "name": filename, "status": "duplicate"}
    langs_on = kb_languages(s)
    fb = fallback_language(s)
    if not langs or len(langs) != len(pages) or not all(lg in language.LABELS for lg in langs):
        language_ = language_ or language.detect(" ".join(t for _, t in pages[:50]), langs_on, fb)
        langs = [language.detect(t, langs_on, language_) for _, t in pages]
    language_ = language_ or max(set(langs), key=langs.count)
    doc_id = await kbdb.create_document(filename, mimetypes.guess_type(filename)[0], sha, size or 0,
                                        has_original=original is not None, metadata={"source": source})
    if original is not None:
        save_original(doc_id, original)
    await kbdb.store_chunks(doc_id, pages, langs, None, None, num_pages, language_,
                            {"ocr_pages": list(ocr_pages or [])})
    if background:
        start_reembed()
    return {"id": doc_id, "name": filename, "status": "imported", "chunks": len(pages)}


async def reindex(doc_id: int):
    """Ripete l'indicizzazione dal file originale; senza originale (documenti importati) ricalcola solo gli
    embedding dei passaggi già presenti."""
    doc = await kbdb.get_document(doc_id)
    if not doc:
        return
    if doc["has_original"] and read_original(doc_id) is not None:
        await ingest(doc_id)
    else:
        await kbdb.execute("UPDATE chunks SET embedding=NULL WHERE document_id=%s", (doc_id,))
        if doc["num_chunks"]:
            await kbdb.set_status(doc_id, "ready")
        else:
            await kbdb.set_status(doc_id, "error", "File originale non disponibile: ricaricalo.")
        start_reembed()


# ---------------- Ricalcolo degli embedding ----------------
_reembed = {"task": None, "error": "", "done": 0}


def start_reembed():
    t = _reembed["task"]
    if t is None or t.done():
        _reembed["task"] = asyncio.get_running_loop().create_task(reembed_pending())


def reembed_running() -> bool:
    t = _reembed["task"]
    return bool(t and not t.done())


async def reembed_pending(progress=None) -> int:
    """Calcola gli embedding mancanti (dopo un cambio di modello o un'importazione)."""
    _reembed.update(error="", done=0)
    try:
        while True:
            s = db.get_settings()
            meta = await kbdb.embedding_meta()
            if not meta or meta.get("model") != s["embed_model"]:
                await kbdb.switch_embedding(s["embed_model"],
                                            int((await embed(["Nexus: verifica del modello."], s)).shape[1]))
                continue
            rows = await kbdb.chunks_without_embedding(64)
            if not rows:
                break
            try:
                vecs = await embed([_doc_text(r["filename"], r["content"]) for r in rows], s)
            except Exception as e:  # noqa: BLE001
                _reembed["error"] = explain_embed_error(e, s)
                log.warning("Ricalcolo embedding interrotto: %s", _reembed["error"])
                return _reembed["done"]
            if vecs.shape[1] != meta["dim"]:
                await kbdb.switch_embedding(s["embed_model"], int(vecs.shape[1]))
                continue
            if await kbdb.update_embeddings([(r["id"], v) for r, v in zip(rows, vecs)], meta):
                _reembed["done"] += len(rows)
                if progress:
                    progress(_reembed["done"])
    except Exception as e:  # noqa: BLE001
        _reembed["error"] = f"{type(e).__name__}: {e}"
        log.exception("Ricalcolo embedding fallito")
    return _reembed["done"]


async def kb_status() -> dict:
    st = await kbdb.stats()
    meta = await kbdb.embedding_meta()
    return {**st, "embedding": meta, "reembed_running": reembed_running(), "reembed_error": _reembed["error"],
            "reembed_done": _reembed["done"]}


# ---------------- Ricerca ----------------
def followup_query(question: str, history_rows) -> str | None:
    """Per le domande di follow-up brevi ("e la scadenza?") restituisce la domanda unita alla precedente,
    da cercare IN AGGIUNTA alla domanda da sola (con peso minore): così "e l'articolo 105?" trova
    l'articolo anche se la domanda precedente parlava d'altro."""
    last_user = next((r["content"] for r in reversed(history_rows or []) if r.get("role") == "user"), None)
    if last_user and len(question.split()) < 8:
        return f"{last_user}\n{question}"
    return None


async def _branches(query: str, s, n, document_ids, meta, state):
    """Esegue ricerca vettoriale e per parole per una domanda; restituisce (vec_hits, kw_hits)."""
    qvec, vec_hits = None, []
    if state["vector_ok"]:
        try:
            qvec = (await embed([query], s))[0]
        except Exception as e:  # noqa: BLE001
            state["vector_ok"] = False
            state["notice"] = f"ricerca semantica non disponibile ({explain_embed_error(e, s)}): uso solo quella per parole"
        if qvec is not None:
            if qvec.shape[0] != meta.get("dim"):
                start_reembed()
                qvec, state["vector_ok"] = None, False
                state["notice"] = "il modello di embedding è cambiato: ricalcolo in corso, uso solo la ricerca per parole"
            else:
                vec_hits = await kbdb.vector_search(qvec, n, document_ids)
    try:
        kw_hits = await kbdb.keyword_search(query, kb_languages(s), n, document_ids, qvec)
    except Exception:  # noqa: BLE001
        log.exception("Ricerca per parole fallita, uso solo la ricerca vettoriale")
        kw_hits = []
    return vec_hits, kw_hits


async def search(query: str, k: int = 5, min_score: float = 0.0, document_ids=None,
                 notices: list | None = None, alt_query: str | None = None) -> list[dict]:
    """Ricerca ibrida con Reciprocal Rank Fusion tra similarità vettoriale e ricerca per parole (BM25).
    min_score si applica alla similarità coseno; un passaggio trovato per parole resta anche sotto soglia se
    contiene le parole più specifiche della domanda (codici, numeri di articolo, nomi propri).
    alt_query: domanda di follow-up unita alla precedente, cercata in aggiunta con peso minore."""
    s = db.get_settings()
    k = max(1, int(k))
    n = max(int(s.get("rag_candidates") or 30), k)
    meta = await kbdb.embedding_meta()
    state = {"vector_ok": bool(meta and meta.get("model") == s["embed_model"]), "notice": ""}
    if meta and not state["vector_ok"]:
        start_reembed()
        state["notice"] = "il modello di embedding è cambiato: ricalcolo in corso, uso solo la ricerca per parole"

    runs = [(query, 1.0)] + ([(alt_query, 0.5)] if alt_query and alt_query != query else [])
    fused: dict[int, dict] = {}
    for q, weight in runs:
        vec_hits, kw_hits = await _branches(q, s, n, document_ids, meta, state)
        for source, hits in (("vector", vec_hits), ("keyword", kw_hits)):
            for rank, hit in enumerate(hits):
                entry = fused.setdefault(hit["id"], {**hit, "rrf": 0.0, "matched": [], "coverage": 0.0})
                # un passaggio che contiene TUTTI i termini specifici della domanda (numeri, codici, nomi)
                # vale fino al doppio: è proprio il caso in cui gli embedding sbagliano più spesso
                boost = 1.0 + float(hit.get("coverage") or 0) if source == "keyword" else 1.0
                entry["rrf"] += weight * boost / (60 + rank + 1)
                if source not in entry["matched"]:
                    entry["matched"].append(source)
                if source == "keyword" and weight == 1.0:
                    entry["coverage"] = max(entry["coverage"], float(hit.get("coverage") or 0))
                if entry.get("similarity") is None and hit.get("similarity") is not None:
                    entry["similarity"] = hit["similarity"]
    ranked = sorted(fused.values(), key=lambda h: h["rrf"], reverse=True)
    out = []
    for h in ranked:
        sim = float(h["similarity"]) if h.get("similarity") is not None else None
        # sotto soglia restano solo i passaggi che contengono la parte più specifica della domanda
        if sim is not None and sim < min_score and h["coverage"] < 0.6:
            continue
        out.append({
            "chunk_id": h["id"], "doc_id": h["document_id"], "name": h["filename"], "page": h["page"],
            "idx": h["chunk_index"], "text": h["content"], "lang": h["lang"], "ocr": bool(h["ocr"]),
            "has_original": bool(h["has_original"]),
            "score": round(sim, 4) if sim is not None else None, "rrf": round(h["rrf"], 5), "matched": h["matched"],
        })
        if len(out) >= k:
            break
    if state["notice"]:
        log.info("Ricerca: %s", state["notice"])
        if notices is not None:
            notices.append(state["notice"])
    return out


def context_block(hits: list[dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, start=1):
        where = h["name"] + (f", pag. {h['page']}" if h["page"] else "")
        where += f", lingua: {language.LABELS.get(h['lang'], h['lang'])}" + (", testo da OCR" if h["ocr"] else "")
        blocks.append(f"[{i}] (fonte: {where})\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


RAG_RULES = ("Gli estratti sono trovati in automatico e possono non essere pertinenti. Se contengono la risposta, "
             "basati su di essi senza inventare dati e cita le fonti con [n] subito dopo l'affermazione che supportano; "
             "se si contraddicono segnalalo. Se NON contengono la risposta, ignorali e rispondi normalmente con le tue "
             "conoscenze, senza dire che l'informazione non è nei documenti e senza citarli. Gli estratti possono essere in lingue diverse: rispondi nella lingua della "
             "domanda traducendo il contenuto, e lascia in originale termini tecnici, nomi propri e citazioni "
             "testuali. Gli estratti marcati «testo da OCR» vengono da scansioni e possono contenere errori di "
             "riconoscimento: se un importo, una data o un numero sembra dubbio, segnalalo.")
