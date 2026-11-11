"""RAG: estrazione testo, chunking, embedding (endpoint OpenAI-compatibile) e ricerca vettoriale su SQLite."""
import io
import os
import re
import threading

import numpy as np

from . import db
from .llm import embed_client

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def file_path(doc_id: int) -> str:
    return os.path.join(UPLOAD_DIR, f"{doc_id}.bin")


def save_original(doc_id: int, data: bytes):
    with open(file_path(doc_id), "wb") as f:
        f.write(data)


def delete_original(doc_id: int):
    try:
        os.remove(file_path(doc_id))
    except FileNotFoundError:
        pass


def explain_embed_error(e: Exception, settings) -> str:
    """Trasforma gli errori più comuni in messaggi con la soluzione."""
    msg = f"{type(e).__name__}: {e}"
    low = str(e).lower()
    model = settings["embed_model"]
    if "not found" in low and "model" in low:
        return (f"Il modello di embedding '{model}' non è installato sull'endpoint. "
                f"Con Ollama esegui: ollama pull {model} — poi premi Riprova.")
    if "does not support embed" in low or "not an embedding" in low:
        return f"Il modello '{model}' non supporta gli embedding: scegline uno dedicato (es. nomic-embed-text, bge-m3)."
    if "connect" in low or "connection" in low:
        return f"Endpoint di embedding non raggiungibile ({settings.get('embed_base_url') or settings['llm_base_url']}): {msg}"
    return msg

_cache_lock = threading.Lock()
_cache = {"valid": False, "ids": None, "doc_ids": None, "matrix": None}


def invalidate_cache():
    with _cache_lock:
        _cache["valid"] = False


# ---------------- Estrazione testo ----------------
def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    text = data.decode("utf-8", errors="ignore")
    if name.endswith((".html", ".htm")):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def chunk_text(text: str, size: int, overlap: int):
    """Chunking ricorsivo: preferisce spezzare su paragrafi, poi frasi, poi spazi."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    size = max(200, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > size * 0.5:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


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


async def ingest(doc_id: int, filename: str, data: bytes):
    settings = db.get_settings()
    db.x("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    db.x("UPDATE documents SET status='processing', error='' WHERE id=?", (doc_id,))
    try:
        try:
            text = extract_text(filename, data)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Impossibile leggere il file: {type(e).__name__}: {e}") from e
        pieces = chunk_text(text, settings["chunk_size"], settings["chunk_overlap"])
        if not pieces:
            raise ValueError("Nessun testo estraibile: il PDF è probabilmente una scansione (immagine) e servirebbe l'OCR.")
        try:
            vectors = await embed(pieces, settings)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(explain_embed_error(e, settings)) from e
        db.xmany("INSERT INTO chunks(doc_id, idx, text, embedding) VALUES(?,?,?,?)",
                 [(doc_id, i, p, v.tobytes()) for i, (p, v) in enumerate(zip(pieces, vectors))])
        db.x("UPDATE documents SET status='ready', chunks=?, embed_model=?, error='' WHERE id=?",
             (len(pieces), settings["embed_model"], doc_id))
    except Exception as e:  # noqa: BLE001
        err = str(e) if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"
        db.x("UPDATE documents SET status='error', error=? WHERE id=?", (err, doc_id))
    finally:
        invalidate_cache()


# ---------------- Ricerca ----------------
def _load_matrix():
    with _cache_lock:
        if _cache["valid"]:
            return _cache
        rows = db.q("SELECT c.id, c.doc_id, c.embedding FROM chunks c JOIN documents d ON d.id=c.doc_id "
                    "WHERE d.status='ready' AND d.enabled=1")
        if rows:
            # Raggruppa per dimensione: tiene la dimensione più diffusa (evita errori se si cambia modello)
            dims = {}
            for r in rows:
                dims.setdefault(len(r["embedding"]) // 4, []).append(r)
            best = max(dims.values(), key=len)
            _cache["ids"] = np.array([r["id"] for r in best])
            _cache["doc_ids"] = np.array([r["doc_id"] for r in best])
            _cache["matrix"] = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in best])
        else:
            _cache["ids"] = _cache["doc_ids"] = _cache["matrix"] = None
        _cache["valid"] = True
        return _cache


async def search(query: str, k: int = 5, min_score: float = 0.0):
    cache = _load_matrix()
    if cache["matrix"] is None:
        return []
    settings = db.get_settings()
    qv = (await embed([query], settings))[0]
    if qv.shape[0] != cache["matrix"].shape[1]:
        raise ValueError("La dimensione dell'embedding della query non corrisponde a quella dei documenti: "
                         "hai cambiato modello di embedding? Re-indicizza i documenti.")
    scores = cache["matrix"] @ qv
    order = np.argsort(-scores)[: max(1, int(k))]
    results = []
    for i in order:
        if scores[i] < min_score:
            continue
        row = db.q("SELECT c.text, c.idx, d.name, d.id AS doc_id FROM chunks c JOIN documents d ON d.id=c.doc_id "
                   "WHERE c.id=?", (int(cache["ids"][i]),), one=True)
        if row:
            row["score"] = round(float(scores[i]), 4)
            results.append(row)
    return results


async def reindex(doc_id: int):
    doc = db.q("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not doc:
        return
    try:
        with open(file_path(doc_id), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        db.x("UPDATE documents SET status='error', error=? WHERE id=?",
             ("File originale non disponibile (caricato con una versione precedente): ricaricalo.", doc_id))
        return
    await ingest(doc_id, doc["name"], data)
