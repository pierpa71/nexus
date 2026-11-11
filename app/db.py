"""Persistenza su SQLite: impostazioni, server MCP, tool, documenti, chunk e conversazioni."""
import json
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "nexus.db")

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mcp_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'http',
    url TEXT DEFAULT '',
    headers TEXT DEFAULT '{}',
    command TEXT DEFAULT '',
    args TEXT DEFAULT '[]',
    env TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'unknown',
    last_error TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS mcp_tools (
    server_id INTEGER NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    input_schema TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    PRIMARY KEY (server_id, name)
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    size INTEGER DEFAULT 0,
    status TEXT DEFAULT 'processing',
    error TEXT DEFAULT '',
    chunks INTEGER DEFAULT 0,
    embed_model TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx INTEGER,
    text TEXT,
    embedding BLOB
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT,
    content TEXT,
    meta TEXT DEFAULT '{}',
    created_at REAL
);
"""

with _lock:
    _conn.executescript(SCHEMA)
    # Migrazioni per DB creati da versioni precedenti
    _cols = {r[1] for r in _conn.execute("PRAGMA table_info(mcp_servers)")}
    if "auth_token" not in _cols:
        _conn.execute("ALTER TABLE mcp_servers ADD COLUMN auth_token TEXT DEFAULT ''")
    if "instructions" not in _cols:
        _conn.execute("ALTER TABLE mcp_servers ADD COLUMN instructions TEXT DEFAULT ''")
    _conn.commit()


def q(sql, params=(), one=False):
    with _lock:
        cur = _conn.execute(sql, params)
        rows = cur.fetchall()
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows


def x(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def xmany(sql, seq):
    with _lock:
        _conn.executemany(sql, seq)
        _conn.commit()


# ---------------- Impostazioni ----------------
DEFAULT_SETTINGS = {
    "llm_base_url": os.environ.get("LLM_BASE_URL", "http://host.docker.internal:11434/v1"),
    "llm_api_key": os.environ.get("LLM_API_KEY", "ollama"),
    "llm_model": os.environ.get("LLM_MODEL", ""),
    "temperature": 0.7,
    "max_tokens": 0,
    "system_prompt": "Sei un assistente personale utile, preciso e conciso. Rispondi nella lingua dell'utente. "
                     "I documenti e gli strumenti che ricevi appartengono all'utente stesso: usali liberamente "
                     "per rispondere alle sue domande, anche su dati personali come fatture, consumi o pagamenti. "
                     "Quando usi informazioni dai documenti, cita la fonte tra parentesi quadre, es. [1].",
    "embed_base_url": os.environ.get("EMBED_BASE_URL", ""),
    "embed_api_key": os.environ.get("EMBED_API_KEY", ""),
    "embed_model": os.environ.get("EMBED_MODEL", "nomic-embed-text"),
    "chunk_size": 1000,
    "chunk_overlap": 150,
    "rag_top_k": 5,
    "rag_min_score": 0.25,
    "rag_inject": "user",             # dove inserire i documenti: "user" (più efficace) o "system"
    "max_tool_rounds": 8,
    "history_max_messages": 20,       # messaggi precedenti inviati al modello (poi ulteriormente ridotti se il contesto è pieno)
    "history_tool_results": True,     # include nello storico le chiamate ai tool e (in breve) i loro risultati
    "history_tool_result_chars": 600, # lunghezza dei risultati dei tool conservati nello storico
    "tool_reminder": True,            # promemoria a ogni messaggio: usa i tool per stati e azioni
    "tool_result_max_chars": 12000,
    "compact_tool_results": True,
    "block_broad_actions": True,
    "ui": {},                         # preferenze di aspetto (tema, colori, sfondo…), gestite dal pannello Aspetto
    # Ricerca web
    "web_search_enabled": False,
    "web_provider": "auto",           # auto | searxng | duckduckgo | brave | tavily
    "searxng_url": os.environ.get("SEARXNG_URL", ""),
    "web_api_key": "",
    "web_max_results": 5,
    "web_fetch_max_chars": 8000,
    "web_allow_private": False,       # consenti a fetch_url di raggiungere indirizzi della rete locale
    "web_language": "it",
    "entity_resolution": True,        # interpreta i nomi approssimativi delle entità (Home Assistant)
    "entity_semantic": True,          # usa anche gli embedding per la somiglianza di significato
    "entity_match_threshold": 0.6,    # soglia di affidabilità per correggere automaticamente il nome      # blocca gli intent Home Assistant senza name/area (agirebbero su tutto)     # estrae il testo dai JSON e compatta il Live Context di Home Assistant   # i risultati più lunghi vengono troncati (evita di saturare il contesto)
    "use_server_instructions": True,  # passa al modello le istruzioni fornite dai server MCP
    "context_window": 8192,           # finestra di contesto del modello, solo per l'avviso di saturazione
    # Thinking: "auto" = non invia nulla (decide il modello), "on" = forza, "off" = disattiva
    "thinking": "auto",
    "reasoning_effort": "medium",
    # Metodi usati per comandare il thinking (dipendono dal backend)
    "think_reasoning_effort": True,    # reasoning_effort: none/low/medium/high  (Ollama /v1, llama.cpp, OpenAI)
    "think_template_kwargs": False,    # chat_template_kwargs.enable_thinking     (vLLM, SGLang, llama.cpp)
    "think_ollama_flag": False,        # think: true/false                        (Ollama, campo nativo)
    "think_prompt_tag": False,         # /think - /no_think nel prompt            (Qwen3, versioni datate)
}


_OLD_SYSTEM_PROMPTS = {
    "Sei un assistente utile, preciso e conciso. Rispondi nella lingua dell'utente. "
    "Quando usi informazioni dai documenti forniti, cita la fonte tra parentesi quadre, es. [1].",
}


def _migrate_settings():
    # aggiorna il prompt di sistema solo se è rimasto quello di default di una versione precedente
    r = q("SELECT value FROM settings WHERE key='system_prompt'", one=True)
    if r and json.loads(r["value"]) in _OLD_SYSTEM_PROMPTS:
        x("UPDATE settings SET value=? WHERE key='system_prompt'", (json.dumps(DEFAULT_SETTINGS["system_prompt"]),))


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except Exception:
            s[r["key"]] = r["value"]
    return s


def save_settings(data: dict):
    for k, v in data.items():
        if k in DEFAULT_SETTINGS:
            x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (k, json.dumps(v)))


# ---------------- Server MCP ----------------
def _server_row(r):
    if not r:
        return r
    for k in ("headers", "env"):
        r[k] = json.loads(r[k] or "{}")
    r["args"] = json.loads(r["args"] or "[]")
    r["enabled"] = bool(r["enabled"])
    return r


def list_servers():
    servers = [_server_row(r) for r in q("SELECT * FROM mcp_servers ORDER BY id")]
    for s in servers:
        s["tools"] = list_tools(s["id"])
    return servers


def get_server(sid):
    return _server_row(q("SELECT * FROM mcp_servers WHERE id=?", (sid,), one=True))


def list_tools(sid):
    rows = q("SELECT * FROM mcp_tools WHERE server_id=? ORDER BY name", (sid,))
    for r in rows:
        r["input_schema"] = json.loads(r["input_schema"] or "{}")
        r["enabled"] = bool(r["enabled"])
    return rows


def sync_tools(sid, tools):
    """Aggiorna l'elenco dei tool preservando lo stato abilitato/disabilitato già scelto."""
    existing = {t["name"]: t["enabled"] for t in list_tools(sid)}
    names = [t["name"] for t in tools]
    with _lock:
        if names:
            _conn.execute(f"DELETE FROM mcp_tools WHERE server_id=? AND name NOT IN ({','.join('?' * len(names))})",
                          (sid, *names))
        else:
            _conn.execute("DELETE FROM mcp_tools WHERE server_id=?", (sid,))
        for t in tools:
            _conn.execute(
                "INSERT INTO mcp_tools(server_id, name, description, input_schema, enabled) VALUES(?,?,?,?,?) "
                "ON CONFLICT(server_id, name) DO UPDATE SET description=excluded.description, input_schema=excluded.input_schema",
                (sid, t["name"], t.get("description") or "", json.dumps(t.get("input_schema") or {}),
                 int(existing.get(t["name"], True))))
        _conn.commit()


_migrate_settings()


def now():
    return time.time()
