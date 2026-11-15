"""Knowledge base su PostgreSQL + pgvector: schema, documenti, passaggi e ricerca ibrida.

Le impostazioni, i server MCP e le conversazioni restano in SQLite (db.py): qui ci sono solo
documenti e passaggi, con l'embedding (indice HNSW) e il tsvector per la ricerca full-text
(indice GIN, stemming della lingua di ciascun passaggio)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re

from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("nexus.kb")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://nexus:nexus@db:5432/nexus")
_pool: AsyncConnectionPool | None = None
_init_lock = asyncio.Lock()


async def _configure(conn):
    await register_vector_async(conn)
    conn.row_factory = dict_row


async def _ensure_extension(retries: int = 30):
    """Crea l'estensione vector (prima che il pool registri il tipo). Attende l'avvio di PostgreSQL."""
    import psycopg

    for attempt in range(retries):
        try:
            async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            return
        except psycopg.OperationalError as e:
            if attempt == retries - 1:
                raise RuntimeError(f"PostgreSQL non raggiungibile ({_safe_url()}): {e}") from e
            log.info("PostgreSQL non ancora pronto, nuovo tentativo tra 2 s…")
            await asyncio.sleep(2)


def _safe_url() -> str:
    import re
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", DATABASE_URL)


async def pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        async with _init_lock:
            if _pool is None:
                await _ensure_extension()
                p = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, configure=_configure, open=False)
                await p.open()
                _pool = p
    return _pool


async def close():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def fetch(sql, params=None, one=False):
    async with (await pool()).connection() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall() if cur.description else []
    return (rows[0] if rows else None) if one else rows


async def execute(sql, params=None) -> int:
    async with (await pool()).connection() as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


# ---------------- Schema ----------------
async def init_schema():
    async with (await pool()).connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_meta (
                key   TEXT PRIMARY KEY,
                value JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id           BIGSERIAL PRIMARY KEY,
                filename     TEXT NOT NULL,
                mime         TEXT,
                sha256       TEXT UNIQUE NOT NULL,
                size_bytes   BIGINT DEFAULT 0,
                num_pages    INT,
                num_chunks   INT DEFAULT 0,
                language     TEXT,
                languages    TEXT[] DEFAULT '{}',
                metadata     JSONB DEFAULT '{}'::jsonb,
                status       TEXT NOT NULL DEFAULT 'processing',
                error        TEXT DEFAULT '',
                enabled      BOOLEAN NOT NULL DEFAULT true,
                has_original BOOLEAN NOT NULL DEFAULT true,
                forced_lang  TEXT,
                created_at   TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id           BIGSERIAL PRIMARY KEY,
                document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                chunk_index  INT NOT NULL,
                page         INT,
                content      TEXT NOT NULL,
                lang         regconfig NOT NULL DEFAULT 'simple',
                -- dimensione fissata al primo embedding; NULL = passaggio in attesa di (ri)calcolo
                embedding    vector,
                -- ogni passaggio è indicizzato con lo stemming della propria lingua
                tsv          tsvector GENERATED ALWAYS AS (to_tsvector(lang, content)) STORED
            );
            CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
            CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (document_id);
            CREATE INDEX IF NOT EXISTS chunks_pending_idx ON chunks (id) WHERE embedding IS NULL;
        """)


# ---------------- Modello di embedding ----------------
async def embedding_meta() -> dict | None:
    r = await fetch("SELECT value FROM kb_meta WHERE key='embedding'", one=True)
    return r["value"] if r else None


async def column_dim() -> int | None:
    r = await fetch("SELECT atttypmod FROM pg_attribute WHERE attrelid='chunks'::regclass AND attname='embedding'",
                    one=True)
    return r["atttypmod"] if r and r["atttypmod"] > 0 else None


async def switch_embedding(model: str, dim: int):
    """Adotta un modello di embedding: se la dimensione cambia (o non era ancora fissata) ricrea la colonna
    e l'indice HNSW; se cambia solo il modello azzera i vettori. Testi e documenti restano intatti:
    i vettori mancanti vengono ricalcolati dal processo di ricalcolo."""
    dim = int(dim)
    async with (await pool()).connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(4242)")
            cur = await conn.execute("SELECT value FROM kb_meta WHERE key='embedding' FOR UPDATE")
            row = await cur.fetchone()
            meta = row["value"] if row else None
            cur = await conn.execute("SELECT atttypmod FROM pg_attribute "
                                     "WHERE attrelid='chunks'::regclass AND attname='embedding'")
            coldim = (await cur.fetchone())["atttypmod"]
            if meta and meta.get("model") == model and meta.get("dim") == dim and coldim == dim:
                return False
            if coldim != dim:
                await conn.execute("DROP INDEX IF EXISTS chunks_embedding_idx")
                await conn.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({dim}) USING NULL")
                await conn.execute("CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)")
            else:
                await conn.execute("UPDATE chunks SET embedding = NULL WHERE embedding IS NOT NULL")
            await conn.execute("""INSERT INTO kb_meta(key, value) VALUES('embedding', %s)
                                  ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value""",
                               (json.dumps({"model": model, "dim": dim}),))
    log.info("Knowledge base: modello di embedding %s (dimensione %d)", model, dim)
    return True


# ---------------- Documenti ----------------
DOC_COLS = """id, filename, mime, sha256, size_bytes, num_pages, num_chunks, language, languages, status, error,
              enabled, has_original, forced_lang, created_at,
              COALESCE(jsonb_array_length(metadata->'ocr_pages'), 0) AS ocr_pages, metadata->>'source' AS source"""


async def list_documents():
    rows = await fetch(f"""SELECT {DOC_COLS},
                              (SELECT count(*) FROM chunks c WHERE c.document_id = d.id AND c.embedding IS NULL)
                                  AS pending_embeddings
                           FROM documents d ORDER BY created_at DESC""")
    for r in rows:
        r["created_at"] = r["created_at"].timestamp() if r["created_at"] else None
    return rows


async def get_document(doc_id: int):
    return await fetch(f"SELECT {DOC_COLS} FROM documents WHERE id=%s", (doc_id,), one=True)


async def find_by_hash(sha: str):
    return await fetch(f"SELECT {DOC_COLS} FROM documents WHERE sha256=%s", (sha,), one=True)


async def create_document(filename, mime, sha, size, has_original=True, forced_lang=None, metadata=None) -> int:
    r = await fetch("""INSERT INTO documents(filename, mime, sha256, size_bytes, has_original, forced_lang, metadata)
                       VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (filename, mime, sha, size, has_original, forced_lang, json.dumps(metadata or {})), one=True)
    return r["id"]


async def set_status(doc_id: int, status: str, error: str = ""):
    await execute("UPDATE documents SET status=%s, error=%s WHERE id=%s", (status, error, doc_id))


async def set_enabled(doc_id: int, enabled: bool):
    await execute("UPDATE documents SET enabled=%s WHERE id=%s", (enabled, doc_id))


async def delete_document(doc_id: int) -> bool:
    return await execute("DELETE FROM documents WHERE id=%s", (doc_id,)) > 0


async def store_chunks(doc_id, chunks, langs, embeddings, signature, num_pages, language, metadata) -> bool:
    """Sostituisce i passaggi del documento. chunks: [(pagina, testo)]; embeddings allineati (o None).
    signature: {"model", "dim"} con cui sono stati calcolati i vettori; se nel frattempo il modello è
    cambiato, i passaggi vengono salvati senza embedding (verranno ricalcolati). Restituisce True in quel caso."""
    async with (await pool()).connection() as conn:
        async with conn.transaction():
            # si serializza con switch_embedding, che modifica la colonna e la riga di kb_meta
            await conn.execute("SELECT pg_advisory_xact_lock_shared(4242)")
            cur = await conn.execute("SELECT value FROM kb_meta WHERE key='embedding'")
            row = await cur.fetchone()
            meta = row["value"] if row else None
            stale = embeddings is None or not meta or not signature or \
                meta.get("model") != signature.get("model") or meta.get("dim") != signature.get("dim")
            vecs = [None] * len(chunks) if stale else list(embeddings)
            await conn.execute("DELETE FROM chunks WHERE document_id=%s", (doc_id,))
            async with conn.cursor() as c:
                await c.executemany(
                    """INSERT INTO chunks(document_id, chunk_index, page, content, lang, embedding)
                       VALUES(%s,%s,%s,%s,%s::regconfig,%s)""",
                    [(doc_id, i, page, text, lg, v) for i, ((page, text), lg, v) in enumerate(zip(chunks, langs, vecs))])
            await conn.execute(
                """UPDATE documents SET num_chunks=%s, num_pages=%s, language=%s, languages=%s,
                          metadata = metadata || %s::jsonb, status='ready', error='' WHERE id=%s""",
                (len(chunks), num_pages, language, sorted(set(langs)), json.dumps(metadata or {}), doc_id))
    return stale


async def chunks_without_embedding(limit: int):
    return await fetch("""SELECT c.id, c.content, d.filename FROM chunks c JOIN documents d ON d.id=c.document_id
                          WHERE c.embedding IS NULL ORDER BY c.id LIMIT %s""", (limit,))


async def count_without_embedding() -> int:
    return (await fetch("SELECT count(*) AS n FROM chunks WHERE embedding IS NULL", one=True))["n"]


async def update_embeddings(pairs, signature) -> bool:
    """Scrive i vettori solo se il modello è ancora quello con cui sono stati calcolati."""
    async with (await pool()).connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock_shared(4242)")
            cur = await conn.execute("SELECT value FROM kb_meta WHERE key='embedding'")
            row = await cur.fetchone()
            meta = row["value"] if row else None
            if not meta or meta.get("model") != signature["model"] or meta.get("dim") != signature["dim"]:
                return False
            async with conn.cursor() as c:
                await c.executemany("UPDATE chunks SET embedding=%s WHERE id=%s", [(v, i) for i, v in pairs])
    return True


async def stats():
    return await fetch("""SELECT (SELECT count(*) FROM documents) AS documents,
                                 (SELECT count(*) FROM documents WHERE status='ready' AND enabled) AS active,
                                 (SELECT count(*) FROM chunks) AS chunks,
                                 (SELECT count(*) FROM chunks WHERE embedding IS NULL) AS pending""", one=True)


async def has_searchable() -> bool:
    return bool(await fetch("SELECT 1 AS x FROM documents WHERE status='ready' AND enabled LIMIT 1", one=True))


# ---------------- Ricerca ----------------
def _filter(document_ids):
    where = " AND d.status='ready' AND d.enabled"
    params = {}
    if document_ids:
        where += " AND c.document_id = ANY(%(doc_ids)s)"
        params["doc_ids"] = [int(i) for i in document_ids]
    return where, params


_SELECT = """c.id, c.document_id, c.chunk_index, c.page, c.content, c.lang::text AS lang, d.filename, d.has_original,
             COALESCE((d.metadata->'ocr_pages') @> to_jsonb(c.page), false) AS ocr"""


async def vector_search(qvec, limit, document_ids=None):
    where, params = _filter(document_ids)
    async with (await pool()).connection() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.ef_search = 100")
            # con i filtri (documenti attivi o scelti) l'indice HNSW deve continuare a scorrere finché non
            # trova abbastanza passaggi validi (pgvector >= 0.8); l'ordine si ristabilisce sotto
            try:
                async with conn.transaction():
                    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            except Exception:  # noqa: BLE001 — pgvector < 0.8
                pass
            cur = await conn.execute(
                f"""WITH hits AS MATERIALIZED (
                        SELECT {_SELECT}, c.embedding <=> %(q)s AS distance
                        FROM chunks c JOIN documents d ON d.id = c.document_id
                        WHERE c.embedding IS NOT NULL {where}
                        ORDER BY distance LIMIT %(limit)s)
                    SELECT *, 1 - distance AS similarity FROM hits ORDER BY distance""",
                {"q": qvec, "limit": limit, **params})
            return await cur.fetchall()


# abbreviazioni frequenti nei documenti (leggi, contratti, fatture): la domanda e il testo possono usare
# forme diverse ("articolo 105" / "Art. 105"); i lessemi del gruppo valgono come la stessa parola
SYNONYMS = [{"art", "articol", "articl", "articles", "articl"}, {"comm", "c"}, {"num", "numer", "n", "nr"},
            {"pag", "pagin", "p"}, {"fattur", "ft"}, {"dlgs", "decret", "d.lgs"}]

_TOKEN = re.compile(r"[\w][\w\-./]*[\w]|[\w]", re.UNICODE)


async def _query_terms(conn, query: str, languages) -> list[list[str]]:
    """Scompone la domanda in termini: per ogni parola i lessemi in tutte le lingue abilitate.
    Le parole vuote (stopword) in almeno una lingua vengono scartate, così "di", "che", "the" non pesano."""
    tokens = list(dict.fromkeys(t.lower() for t in _TOKEN.findall(query)))[:30]
    if not tokens:
        return []
    groups = {t: set() for t in tokens}
    stop = set()
    for lg in languages:
        cur = await conn.execute(
            "SELECT t, tsvector_to_array(to_tsvector(%s::regconfig, t)) AS lex FROM unnest(%s::text[]) AS t",
            (lg, tokens))
        for r in await cur.fetchall():
            if not r["lex"]:
                stop.add(r["t"])
            groups[r["t"]].update(r["lex"])
    out = []
    for t in tokens:
        if t in stop or not groups[t]:
            continue
        g = set(groups[t])
        for syn in SYNONYMS:
            if g & syn:
                g |= syn
        if g not in out:
            out.append(g)
    return [sorted(g) for g in out]


def _tsq(lexemes) -> str:
    return " | ".join("'" + lx.replace("'", "''").replace("\\", "") + "'" for lx in lexemes)


async def keyword_search(query, languages, limit, document_ids=None, qvec=None):
    """Ricerca per parole stile BM25: un passaggio non deve contenere TUTTE le parole della domanda;
    ogni parola trovata pesa in base alla sua rarità nell'archivio (idf), quindi un numero di articolo,
    un codice o un nome proprio contano molto più di parole comuni come "articolo" o "documento".
    Ogni passaggio è indicizzato con lo stemming della propria lingua; le parole della domanda vengono
    analizzate con tutte le lingue abilitate."""
    where, params = _filter(document_ids)
    async with (await pool()).connection() as conn:
        groups = await _query_terms(conn, query, languages)
        if not groups:
            return []
        gp = {f"g{i}": _tsq(g) for i, g in enumerate(groups)}
        anyq = _tsq(sorted({lx for g in groups for lx in g}))
        counts = ", ".join(f"count(*) FILTER (WHERE c.tsv @@ %(g{i})s::tsquery) AS d{i}" for i in range(len(groups)))
        cur = await conn.execute(
            f"SELECT count(*) AS n, {counts} FROM chunks c JOIN documents d ON d.id = c.document_id WHERE true {where}",
            {**gp, **params})
        st = await cur.fetchone()
        n = st["n"] or 0
        if not n:
            return []
        idf = [math.log((n - st[f"d{i}"] + 0.5) / (st[f"d{i}"] + 0.5) + 1) for i in range(len(groups))]
        score = " + ".join(f"CASE WHEN c.tsv @@ %(g{i})s::tsquery THEN {idf[i]:.6f} ELSE 0 END" for i in range(len(groups)))
        sim = "CASE WHEN c.embedding IS NULL THEN NULL ELSE 1 - (c.embedding <=> %(q)s) END" if qvec is not None else "NULL"
        best = max(idf)
        cur = await conn.execute(
            f"""WITH m AS (
                    SELECT {_SELECT}, ({score}) AS rank, ({score}) / %(total)s AS coverage, ts_rank_cd(c.tsv, %(anyq)s::tsquery) AS density, {sim} AS similarity
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE c.tsv @@ %(anyq)s::tsquery {where})
                SELECT * FROM m
                -- servono le parole più specifiche: scarta i passaggi che hanno solo termini comuni
                WHERE rank >= %(min_rank)s
                ORDER BY rank DESC, density DESC LIMIT %(limit)s""",
            {**gp, **params, "anyq": anyq, "q": qvec, "limit": limit,
             "min_rank": min(best, sum(idf)) * 0.5, "total": sum(idf) or 1.0})
        return await cur.fetchall()
