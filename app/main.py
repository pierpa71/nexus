"""Nexus Chat — API FastAPI + frontend statico."""
import asyncio
import json
import os
import uuid

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import BadRequestError
from pydantic import BaseModel

from . import db, entities, mcp_client, rag, websearch
from .llm import chat_client, ollama_runtime

APP_VERSION = "1.13.0"
app = FastAPI(title="Nexus Chat", version=APP_VERSION)
STATIC = os.path.join(os.path.dirname(__file__), "static")
MASK = "••••••••"
SECRET_KEYS = ("llm_api_key", "embed_api_key", "web_api_key")


# ======================= Impostazioni =======================
@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    for k in SECRET_KEYS:
        if s.get(k):
            s[k] = MASK + str(s[k])[-4:]
    return s


@app.put("/api/settings")
def put_settings(data: dict):
    for k in SECRET_KEYS:
        if isinstance(data.get(k), str) and data[k].startswith(MASK):
            data.pop(k)  # valore mascherato: mantieni quello salvato
    db.save_settings(data)
    rag.invalidate_cache()
    return get_settings()


@app.get("/api/models")
async def list_models(target: str = "llm"):
    s = db.get_settings()
    from .llm import embed_client
    client = chat_client(s) if target == "llm" else embed_client(s)
    try:
        res = await client.models.list()
        return {"models": sorted(m.id for m in res.data)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Impossibile leggere i modelli: {e}")


# ======================= MCP =======================
class ServerIn(BaseModel):
    name: str
    transport: str = "http"
    url: str = ""
    headers: dict = {}
    command: str = ""
    args: list = []
    env: dict = {}
    auth_token: str = ""
    enabled: bool = True


def _public_server(s: dict) -> dict:
    tok = s.pop("auth_token", "") or ""
    s["has_token"] = bool(tok)
    s["auth_token"] = (MASK + tok[-4:]) if tok else ""
    return s


@app.get("/api/mcp/servers")
def mcp_servers():
    return [_public_server(s) for s in db.list_servers()]


@app.post("/api/mcp/servers")
async def mcp_add(s: ServerIn):
    sid = db.x("INSERT INTO mcp_servers(name, transport, url, headers, command, args, env, auth_token, enabled, "
               "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (s.name, s.transport, s.url, json.dumps(s.headers), s.command, json.dumps(s.args),
                json.dumps(s.env), s.auth_token.strip(), int(s.enabled), db.now()))
    res = await mcp_client.refresh_server(sid)
    return {"id": sid, **res}


@app.post("/api/mcp/import")
async def mcp_import(data: dict):
    try:
        servers = mcp_client.parse_mcp_config(data.get("config", data))
    except ValueError as e:
        raise HTTPException(400, str(e))
    results = []
    for sv in servers:
        sid = db.x("INSERT INTO mcp_servers(name, transport, url, headers, command, args, env, auth_token, enabled, "
                   "created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                   (sv["name"], sv["transport"], sv["url"], json.dumps(sv["headers"]), sv["command"],
                    json.dumps(sv["args"]), json.dumps(sv["env"]), sv["auth_token"], db.now()))
        res = await mcp_client.refresh_server(sid)
        results.append({"id": sid, "name": sv["name"], "transport": sv["transport"], **res})
    return {"servers": results}


@app.put("/api/mcp/servers/{sid}")
async def mcp_update(sid: int, s: ServerIn):
    old = db.get_server(sid)
    if not old:
        raise HTTPException(404)
    # valore mascherato = token invariato; stringa vuota = token rimosso
    token = (old.get("auth_token") or "") if s.auth_token.startswith(MASK) else s.auth_token.strip()
    db.x("UPDATE mcp_servers SET name=?, transport=?, url=?, headers=?, command=?, args=?, env=?, auth_token=?, "
         "enabled=? WHERE id=?",
         (s.name, s.transport, s.url, json.dumps(s.headers), s.command, json.dumps(s.args), json.dumps(s.env),
          token, int(s.enabled), sid))
    return await mcp_client.refresh_server(sid)


@app.patch("/api/mcp/servers/{sid}")
def mcp_toggle(sid: int, data: dict):
    db.x("UPDATE mcp_servers SET enabled=? WHERE id=?", (int(bool(data.get("enabled"))), sid))
    return {"ok": True}


@app.delete("/api/mcp/servers/{sid}")
def mcp_delete(sid: int):
    db.x("DELETE FROM mcp_servers WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/mcp/servers/{sid}/refresh")
async def mcp_refresh(sid: int):
    return await mcp_client.refresh_server(sid)


@app.post("/api/mcp/servers/{sid}/tools/{name}/call")
async def mcp_tool_call(sid: int, name: str, data: dict):
    srv = db.get_server(sid)
    if not srv:
        raise HTTPException(404)
    args = data.get("args") or {}
    import time as _t
    t0 = _t.time()
    out, err = await mcp_client.call_tool(srv, name, args)
    return {"result": out, "error": err, "ms": int((_t.time() - t0) * 1000), "chars": len(out)}


@app.get("/api/mcp/servers/{sid}/catalog")
async def mcp_catalog(sid: int, refresh: bool = False):
    srv = db.get_server(sid)
    if not srv:
        raise HTTPException(404)
    srv["tools"] = db.list_tools(sid)
    if not entities.server_supports(srv):
        return {"supported": False}
    ents = await entities.get_catalog(srv, force=refresh)
    return {"supported": True, "count": len(ents),
            "entities": [{k: e[k] for k in ("name", "aliases", "domain", "area")} for e in ents]}


@app.post("/api/mcp/servers/{sid}/resolve")
async def mcp_resolve(sid: int, data: dict):
    """Prova la risoluzione di un nome (diagnostica dal pannello MCP)."""
    srv = db.get_server(sid)
    if not srv:
        raise HTTPException(404)
    srv["tools"] = db.list_tools(sid)
    args, note, block = await entities.resolve(srv, data.get("tool") or "HassTurnOff", {"name": data.get("name", "")},
                                               db.get_settings(), data.get("user_text", ""))
    return {"args": args, "note": note, "ambiguous": block}


@app.put("/api/mcp/servers/{sid}/tools")
def mcp_tools_bulk(sid: int, data: dict):
    db.x("UPDATE mcp_tools SET enabled=? WHERE server_id=?", (int(bool(data.get("enabled"))), sid))
    return {"ok": True}


@app.put("/api/mcp/servers/{sid}/tools/{name}")
def mcp_tool_toggle(sid: int, name: str, data: dict):
    db.x("UPDATE mcp_tools SET enabled=? WHERE server_id=? AND name=?", (int(bool(data.get("enabled"))), sid, name))
    return {"ok": True}


# ======================= Documenti / RAG =======================
@app.get("/api/documents")
def documents():
    return db.q("SELECT * FROM documents ORDER BY created_at DESC")


@app.post("/api/documents")
async def upload(bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        data = await f.read()
        did = db.x("INSERT INTO documents(name, size, status, created_at) VALUES(?,?, 'processing', ?)",
                   (f.filename, len(data), db.now()))
        rag.save_original(did, data)
        bg.add_task(rag.ingest, did, f.filename, data)
        out.append({"id": did, "name": f.filename})
    return out


@app.patch("/api/documents/{did}")
def doc_toggle(did: int, data: dict):
    db.x("UPDATE documents SET enabled=? WHERE id=?", (int(bool(data.get("enabled"))), did))
    rag.invalidate_cache()
    return {"ok": True}


@app.post("/api/documents/{did}/reindex")
def doc_reindex(did: int, bg: BackgroundTasks):
    db.x("UPDATE documents SET status='processing', error='' WHERE id=?", (did,))
    bg.add_task(rag.reindex, did)
    return {"ok": True}


@app.post("/api/documents/reindex")
def doc_reindex_all(bg: BackgroundTasks, data: dict | None = None):
    only_errors = bool((data or {}).get("only_errors"))
    rows = db.q("SELECT id FROM documents" + (" WHERE status='error'" if only_errors else ""))
    for r in rows:
        db.x("UPDATE documents SET status='processing', error='' WHERE id=?", (r["id"],))
        bg.add_task(rag.reindex, r["id"])
    return {"count": len(rows)}


@app.post("/api/web/test")
async def web_test():
    s = db.get_settings()
    try:
        provider, res = await websearch.search("meteo Roma oggi", 3, s)
        return {"ok": True, "provider": provider, "count": len(res), "first": res[0]["title"] if res else ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/embeddings/test")
async def embed_test():
    s = db.get_settings()
    try:
        v = await rag.embed(["test di connessione"], s)
        return {"ok": True, "dim": int(v.shape[1]), "model": s["embed_model"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": rag.explain_embed_error(e, s)}


@app.delete("/api/documents/{did}")
def doc_delete(did: int):
    rag.delete_original(did)
    db.x("DELETE FROM documents WHERE id=?", (did,))
    rag.invalidate_cache()
    return {"ok": True}


@app.post("/api/rag/search")
async def rag_search(data: dict):
    s = db.get_settings()
    try:
        return await rag.search(data.get("query", ""), data.get("k") or s["rag_top_k"], 0)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e))


# ======================= Conversazioni =======================
@app.get("/api/conversations")
def conversations():
    return db.q("SELECT * FROM conversations ORDER BY updated_at DESC")


@app.get("/api/conversations/{cid}")
def conversation(cid: str):
    conv = db.q("SELECT * FROM conversations WHERE id=?", (cid,), one=True)
    if not conv:
        raise HTTPException(404)
    msgs = db.q("SELECT role, content, meta, created_at FROM messages WHERE conv_id=? ORDER BY id", (cid,))
    for m in msgs:
        m["meta"] = json.loads(m["meta"] or "{}")
    return {**conv, "messages": msgs}


@app.patch("/api/conversations/{cid}")
def conv_rename(cid: str, data: dict):
    db.x("UPDATE conversations SET title=? WHERE id=?", (data.get("title", "")[:120], cid))
    return {"ok": True}


@app.delete("/api/conversations/{cid}")
def conv_delete(cid: str):
    db.x("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


# ======================= Chat =======================
class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None
    use_rag: bool = True
    use_tools: bool = True
    model: str | None = None
    thinking: str | None = None  # auto | on | off (override della impostazione)
    web_search: bool | None = None  # override del toggle Web


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_OOM_MARKERS = ("out of memory", "insufficient memory", "outofmemory", "not enough memory",
                "failed to allocate", "requires more system memory")


def explain_llm_error(e: Exception, model: str) -> str:
    raw = f"{type(e).__name__}: {e}"
    low = raw.lower()
    if any(m in low for m in _OOM_MARKERS):
        return (f"Memoria insufficiente sul server del modello: '{model}' più la finestra di contesto non entrano in "
                "RAM/VRAM. Riduci OLLAMA_CONTEXT_LENGTH (es. 8192), imposta OLLAMA_NUM_PARALLEL=1, usa un modello "
                "più piccolo o più quantizzato, oppure disattiva i tool non necessari per accorciare il prompt.\n\n"
                f"Dettaglio: {raw[:300]}")
    if "model" in low and "not found" in low:
        return f"Il modello '{model}' non è installato sull'endpoint (con Ollama: ollama pull {model}).\n\nDettaglio: {raw[:300]}"
    return raw


def now_line() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = os.environ.get("TZ") or "Europe/Rome"
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    return f"Data e ora attuali: {giorni[now.weekday()]} {now:%d/%m/%Y %H:%M} ({tz})."


def est_tokens(obj) -> int:
    return int(len(json.dumps(obj, ensure_ascii=False)) / 3.5)


def build_history(rows, mapping, s, budget):
    """Ricostruisce lo storico per il modello. Per le risposte che avevano usato dei tool include le
    chiamate e un estratto dei risultati, così il modello continua a usarli invece di imitare risposte
    date "a memoria". Elimina i turni più vecchi finché lo storico non rientra nel budget di token."""
    reverse = {(srv["name"], tname): fname for fname, (srv, tname) in mapping.items()}
    keep_tools = s.get("history_tool_results", True)
    max_chars = int(s.get("history_tool_result_chars") or 600)
    turns, cur = [], []
    for r in rows:
        if r["role"] == "user":
            if cur:
                turns.append(cur)
            cur = [{"role": "user", "content": r["content"]}]
            continue
        meta = json.loads(r.get("meta") or "{}")
        msgs = []
        used = meta.get("tools") or []
        if used and keep_tools:
            calls, results = [], []
            for i, t in enumerate(used):
                fname = reverse.get((t.get("server"), t.get("name")))
                cid = f"hist_{len(turns)}_{i}"
                res = str(t.get("result") or "")
                res = res[:max_chars] + ("…" if len(res) > max_chars else "")
                if fname:
                    calls.append({"id": cid, "type": "function",
                                  "function": {"name": fname, "arguments": json.dumps(t.get("args") or {})}})
                    results.append({"role": "tool", "tool_call_id": cid, "content": res})
            if calls:
                msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
                msgs.extend(results)
        elif used:
            names = ", ".join(sorted({t.get("name", "?") for t in used}))
            msgs.append({"role": "assistant", "content": f"[strumenti usati: {names}]"})
        if r["content"]:
            msgs.append({"role": "assistant", "content": r["content"]})
        elif not msgs:
            continue
        cur.extend(msgs)
    if cur:
        turns.append(cur)
    dropped = 0
    while turns and est_tokens([m for t in turns for m in t]) > budget:
        dropped += sum(1 for m in turns[0] if m["role"] in ("user",)) + sum(
            1 for m in turns[0] if m["role"] == "assistant" and m.get("content"))
        turns.pop(0)
    # ogni turno deve iniziare con il messaggio dell'utente (il limite di righe può tagliare a metà turno)
    turns = [t for t in turns if t and t[0]["role"] == "user"]
    return [m for t in turns for m in t], dropped


def thinking_params(s: dict, mode: str):
    """Restituisce (kwargs top-level, extra_body, suffisso_prompt) per comandare il thinking."""
    top, extra, tag = {}, {}, ""
    if mode not in ("on", "off"):
        return top, extra, tag
    on = mode == "on"
    if s.get("think_reasoning_effort"):
        top["reasoning_effort"] = (s.get("reasoning_effort") or "medium") if on else "none"
    if s.get("think_template_kwargs"):
        extra["chat_template_kwargs"] = {"enable_thinking": on}
    if s.get("think_ollama_flag"):
        extra["think"] = on
    if s.get("think_prompt_tag"):
        tag = " /think" if on else " /no_think"
    return top, extra, tag


class ThinkSplitter:
    """Separa i blocchi <think>…</think> inline nel contenuto (es. DeepSeek-R1/Qwen3 su alcuni backend)."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf, self.inside, self.started = "", False, False

    def feed(self, text: str):
        """Restituisce lista di (tipo, testo) con tipo 'content' o 'reasoning'."""
        self.buf += text
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                piece, self.buf = self.buf[:i], self.buf[i + len(tag):]
                if piece:
                    out.append(("reasoning" if self.inside else "content", piece))
                self.inside = not self.inside
                continue
            # trattieni un possibile tag spezzato a fine buffer
            keep = 0
            for k in range(1, len(tag)):
                if self.buf.endswith(tag[:k]):
                    keep = k
            emit = self.buf[:len(self.buf) - keep] if keep else self.buf
            self.buf = self.buf[len(emit):]
            if emit:
                out.append(("reasoning" if self.inside else "content", emit))
            break
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return [("reasoning" if self.inside else "content", rest)] if rest else []


def _extra(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is None and getattr(obj, "model_extra", None):
            v = obj.model_extra.get(n)
        if v:
            return v
    return None


async def chat_stream(req: ChatIn):
    s = db.get_settings()
    model = req.model or s["llm_model"]
    cid = req.conversation_id
    if not cid or not db.q("SELECT id FROM conversations WHERE id=?", (cid,), one=True):
        cid = uuid.uuid4().hex
        title = req.message.strip().replace("\n", " ")[:60] or "Nuova chat"
        db.x("INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?)",
             (cid, title, db.now(), db.now()))
    hist_rows = db.q("SELECT role, content, meta FROM messages WHERE conv_id=? AND role IN ('user','assistant') "
                     "ORDER BY id DESC LIMIT ?", (cid, max(0, int(s.get("history_max_messages") or 20))))[::-1]
    db.x("INSERT INTO messages(conv_id, role, content, created_at) VALUES(?,?,?,?)",
         (cid, "user", req.message, db.now()))
    yield sse("meta", {"conversation_id": cid, "model": model})

    meta = {"tools": [], "sources": [], "reasoning": ""}
    rag_block = ""
    rag_ctx = ""
    full_text = ""
    try:
        if not model:
            raise ValueError("Nessun modello configurato: apri Impostazioni e seleziona un modello.")
        system = s["system_prompt"] + "\n\n" + now_line()

        # ---- RAG ----
        if req.use_rag and db.q("SELECT 1 FROM documents WHERE status='ready' AND enabled=1 LIMIT 1"):
            try:
                hits = await rag.search(req.message, s["rag_top_k"], float(s["rag_min_score"]))
            except Exception as e:  # noqa: BLE001
                hits = []
                yield sse("notice", {"text": f"RAG non disponibile: {e}"})
            if hits:
                ctx = "\n\n".join(f"[{i + 1}] (fonte: {h['name']})\n{h['text']}" for i, h in enumerate(hits))
                rag_ctx = ctx
                if s.get("rag_inject", "user") == "system":
                    system += ("\n\n### Contesto dai documenti della knowledge base\n"
                               "Usa il contesto seguente se pertinente alla domanda e cita le fonti con [n].\n\n" + ctx)
                else:
                    rag_block = ("Estratti dai MIEI documenti personali (fatture, contratti, note…). "
                                 "Sono dati miei: usali per rispondere e cita le fonti con [n]. "
                                 "Se gli estratti non contengono la risposta, dillo.\n\n"
                                 f"<documenti>\n{ctx}\n</documenti>\n\nDomanda: ")
                meta["sources"] = [{"n": i + 1, "name": h["name"], "score": h["score"], "text": h["text"][:400]}
                                   for i, h in enumerate(hits)]
                yield sse("sources", meta["sources"])

        # ---- Tool MCP ----
        tools, mapping, srv_instr = mcp_client.build_openai_tools() if req.use_tools else ([], {}, [])
        web_on = req.web_search if req.web_search is not None else bool(s.get("web_search_enabled"))
        if web_on:
            wt, wm = websearch.tools_and_mapping()
            tools = [*tools, *wt]
            mapping = {**mapping, **wm}
        if tools:
            system += ("\n\n### Strumenti\nHai a disposizione degli strumenti (tool) collegati a sistemi reali. "
                       "Quando la richiesta riguarda dati o azioni che questi strumenti possono fornire "
                       "(es. stato di dispositivi, sensori, sistemi esterni), CHIAMA lo strumento adatto invece di "
                       "rispondere che non hai accesso alle informazioni. Non inventare i risultati.")
            if s.get("use_server_instructions", True):
                for name, instr in srv_instr:
                    system += f"\n\n#### Istruzioni del server MCP «{name}»\n{instr.strip()[:4000]}"
        if web_on:
            system += websearch.system_hint(s)

        mode = req.thinking or s.get("thinking", "auto")
        think_top, think_extra, think_tag = thinking_params(s, mode)

        # ---- Storico: con le chiamate ai tool, ridotto in base al contesto disponibile ----
        reminder = ""
        if tools and s.get("tool_reminder", True):
            reminder = ("\n\n(Promemoria: per conoscere uno stato o eseguire un'azione chiama lo strumento adatto in "
                        "questo turno, anche se l'hai già fatto prima: i dati possono essere cambiati. Indica sempre "
                        "il nome esatto dell'entità. Quando lo strumento conferma il successo, NON ripetere l'azione "
                        "e non fare altre chiamate: rispondi. Non dire mai di aver eseguito un'azione che non hai "
                        "eseguito con uno strumento.)")
        current = {"role": "user", "content": rag_block + req.message + reminder + think_tag}
        oll = await ollama_runtime(s, model)
        cw = int(oll.get("ctx") or s.get("context_window") or 0)
        reserve = int(s.get("max_tokens") or 0) or 1024
        fixed = est_tokens([{"role": "system", "content": system}, current]) + (est_tokens(tools) if tools else 0)
        budget = (cw - fixed - reserve) if cw else 10 ** 9
        history, dropped = build_history(hist_rows, mapping, s, budget)
        if dropped:
            meta["history_dropped"] = dropped
        messages = [{"role": "system", "content": system}, *history, current]
        base_len = len(messages)
        client = chat_client(s)
        think_dropped = False
        ctx_warned = False
        done_ok, tool_errors = set(), {}
        max_res = int(s.get("tool_result_max_chars") or 12000)

        for _round in range(int(s["max_tool_rounds"]) + 1):
            kwargs = {"model": model, "messages": messages, "stream": True,
                      "temperature": float(s["temperature"])}
            if int(s.get("max_tokens") or 0) > 0:
                kwargs["max_tokens"] = int(s["max_tokens"])
            if tools and _round < int(s["max_tool_rounds"]):
                kwargs["tools"] = tools
            # stima grossolana dei token (≈ 3,5 caratteri/token) per avvisare in caso di contesto saturo
            est = int((len(json.dumps(messages, ensure_ascii=False)) + (len(json.dumps(tools)) if "tools" in kwargs else 0)) / 3.5)
            cw = int(oll.get("ctx") or s.get("context_window") or 0)
            if cw and est > cw * 0.85 and not ctx_warned:
                ctx_warned = True
                yield sse("notice", {"text": f"Il prompt è di circa {est:,} token, vicino o oltre la finestra di contesto "
                                             f"impostata ({cw:,}). Il modello potrebbe ignorare istruzioni, tool o risultati. "
                                             "Con Ollama aumenta OLLAMA_CONTEXT_LENGTH; in alternativa disabilita tool "
                                             "non necessari o riduci la lunghezza massima dei risultati."})
            if oll["ollama"]:
                kwargs["stream_options"] = {"include_usage": True}
            if not think_dropped:
                kwargs.update(think_top)
                if think_extra:
                    kwargs["extra_body"] = think_extra
            try:
                stream = await client.chat.completions.create(**kwargs)
            except BadRequestError as e:
                if "does not support tools" in str(e).lower():
                    raise RuntimeError(f"Il modello '{model}' non supporta il function calling, quindi non può usare "
                                       "i tool MCP. Usa un modello con supporto tool (es. qwen3, llama3.1, "
                                       "mistral-nemo, gpt-oss) oppure disattiva «Tool».") from e
                if think_dropped or not (think_top or think_extra):
                    raise
                # il backend non accetta i parametri di thinking: riprova senza e avvisa
                think_dropped = True
                for k in think_top:
                    kwargs.pop(k, None)
                kwargs.pop("extra_body", None)
                yield sse("notice", {"text": "L'endpoint ha rifiutato i parametri di thinking "
                                             f"({str(e)[:160]}): risposta generata senza. "
                                             "Cambia il metodo in Impostazioni → Thinking."})
                stream = await client.chat.completions.create(**kwargs)
            content, calls = "", {}
            splitter = ThinkSplitter()
            usage = None
            finish = None
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                if chunk.choices[0].finish_reason:
                    finish = chunk.choices[0].finish_reason
                d = chunk.choices[0].delta
                if d is None:
                    continue
                r = _extra(d, "reasoning_content", "reasoning")
                if isinstance(r, str) and r:
                    meta["reasoning"] += r
                    yield sse("reasoning", {"text": r})
                if d.content:
                    for kind, txt in splitter.feed(d.content):
                        if kind == "reasoning":
                            meta["reasoning"] += txt
                            yield sse("reasoning", {"text": txt})
                        else:
                            content += txt
                            yield sse("token", {"text": txt})
                for tc in d.tool_calls or []:
                    key = tc.index if tc.index is not None else (tc.id or len(calls))
                    c = calls.setdefault(key, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        c["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            c["name"] += tc.function.name
                        if tc.function.arguments:
                            c["args"] += tc.function.arguments if isinstance(tc.function.arguments, str) \
                                else json.dumps(tc.function.arguments)
            for kind, txt in splitter.flush():
                if kind == "reasoning":
                    meta["reasoning"] += txt
                    yield sse("reasoning", {"text": txt})
                else:
                    content += txt
                    yield sse("token", {"text": txt})
            # ---- utilizzo del contesto (reale da Ollama se disponibile, altrimenti stimato) ----
            if oll["ollama"] and not oll.get("ctx"):
                oll = await ollama_runtime(s, model)  # ora il modello è caricato: leggi il contesto reale
            ctx_len = oll.get("ctx") or int(s.get("context_window") or 0) or None
            rag_tok = est_tokens(rag_ctx) if rag_ctx else 0
            rag_in_sys = bool(rag_ctx) and s.get("rag_inject", "user") == "system"
            parts = {
                "system": max(0, est_tokens(messages[0]) - (rag_tok if rag_in_sys else 0)),
                "tools": est_tokens(tools) if "tools" in kwargs else 0,
                "history": est_tokens(history) if history else 0,
                "rag": rag_tok,
                "message": max(0, est_tokens(current) - (0 if rag_in_sys else rag_tok)),
                "tool_results": est_tokens(messages[base_len:]) if len(messages) > base_len else 0,
            }
            est_total = sum(parts.values()) or 1
            real = getattr(usage, "prompt_tokens", None) if usage else None
            prompt_tok = int(real or est_total)
            k = prompt_tok / est_total
            parts = {key: int(v * k) for key, v in parts.items()}
            truncated = bool(real and oll.get("ctx") and real >= oll["ctx"] - 16)
            meta["usage"] = {"prompt": prompt_tok, "completion": getattr(usage, "completion_tokens", 0) if usage else 0,
                             "ctx": ctx_len, "ctx_source": "ollama" if oll.get("ctx") else ("impostazioni" if ctx_len else None),
                             "estimated": not real, "truncated": truncated, "parts": parts, "model": model,
                             "history_dropped": meta.get("history_dropped", 0), "round": _round + 1}
            yield sse("usage", meta["usage"])
            if True:
                if truncated and not ctx_warned:
                    ctx_warned = True
                    yield sse("notice", {"text": f"⚠ Prompt TRONCATO da Ollama: {prompt_tok:,} token su una "
                                                 f"finestra di {ctx_len:,}. Il modello non ha visto parte delle "
                                                 "istruzioni, dei documenti o dei tool. Avvia Ollama con "
                                                 "OLLAMA_CONTEXT_LENGTH=16384 (o più) oppure disattiva i tool "
                                                 "non necessari."})
            full_text += content
            if not calls:
                if not full_text.strip():
                    if finish == "length":
                        why = ("Il modello ha raggiunto il limite di token prima di scrivere la risposta"
                               + (" (li ha spesi tutti nel ragionamento)" if meta["reasoning"] else "")
                               + ". Aumenta «Max token risposta» (0 = nessun limite) o imposta Thinking su Off.")
                    elif meta["reasoning"]:
                        why = ("Il modello ha prodotto solo il ragionamento, senza una risposta finale. "
                               "Riprova, oppure imposta Thinking su Off o usa un modello più grande.")
                    elif meta.get("usage", {}).get("truncated"):
                        why = "Risposta vuota: il prompt è stato troncato perché supera la finestra di contesto."
                    else:
                        why = (f"Il modello ha restituito una risposta vuota (finish_reason: {finish or 'n/d'}). "
                               "Succede con prompt troppo lunghi per il contesto o con modelli molto piccoli: "
                               "prova a disattivare RAG/Tool per questa domanda o a cambiare modello.")
                    meta["empty_reason"] = why
                    yield sse("empty", {"text": why})
                break

            calls = list(calls.values())
            for c in calls:
                c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:12]}"
            messages.append({"role": "assistant", "content": content or None,
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                            for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                srv, tname = mapping.get(c["name"], (None, c["name"]))
                notes, blocked, resolve_note = [], None, None
                if srv is not None and not srv.get("builtin"):
                    schema = mcp_client.tool_schema(srv["id"], tname)
                    args, notes = mcp_client.clean_args(args, schema)
                    if s.get("entity_resolution", True) and entities.server_supports(srv):
                        try:
                            args, rnote, rblock = await entities.resolve(srv, tname, args, s, req.message)
                        except Exception as e:  # noqa: BLE001
                            rnote, rblock = f"Risoluzione entità non riuscita: {e}", None
                        resolve_note = rnote
                        if rblock:
                            blocked = rblock
                    else:
                        resolve_note = None
                    if not blocked and s.get("block_broad_actions", True):
                        blocked = mcp_client.broad_action_problem(tname, args, schema)
                ev = {"id": c["id"], "name": tname, "server": srv["name"] if srv else "?", "args": args}
                if notes:
                    ev["note"] = "Argomenti corretti: rimossi " + ", ".join(notes)
                if srv is not None and resolve_note:
                    ev["note"] = (ev.get("note", "") + " · " if ev.get("note") else "") + resolve_note
                sig = (c["name"], json.dumps(args, sort_keys=True))
                yield sse("tool_call", ev)
                if srv is None:
                    out, err = f"Tool '{c['name']}' non disponibile o disabilitato.", True
                elif blocked:
                    out, err = blocked, True
                    if not resolve_note:
                        ev["note"] = (ev.get("note", "") + " · " if ev.get("note") else "") + "Bloccata: azione senza destinatario"
                elif sig in done_ok:
                    out, err = ("Questa identica chiamata è GIÀ stata eseguita con successo in questo turno: non "
                                "ripeterla. Rispondi all'utente con l'esito."), False
                    ev["note"] = (ev.get("note", "") + " · " if ev.get("note") else "") + "Non rieseguita: già completata"
                elif tool_errors.get(c["name"], 0) >= 2:
                    out, err = ("Questo strumento ha già dato errore 2 volte in questo turno: NON richiamarlo. "
                                "Spiega all'utente cosa è andato storto."), True
                    ev["note"] = (ev.get("note", "") + " · " if ev.get("note") else "") + "Non eseguita: troppi errori"
                else:
                    if srv.get("builtin"):
                        out, err = await websearch.run(tname, args, s)
                    else:
                        out, err = await mcp_client.call_tool(srv, tname, args)
                    if err:
                        tool_errors[c["name"]] = tool_errors.get(c["name"], 0) + 1
                    else:
                        done_ok.add(sig)
                if len(out) > max_res:
                    out = (out[:max_res] + f"\n\n[…risultato troncato: {len(out):,} caratteri totali, mostrati "
                           f"{max_res:,}. Se serve, richiama lo strumento con filtri più specifici.]")
                ev = {**ev, "result": out[:20000], "error": err}
                meta["tools"].append(ev)
                yield sse("tool_result", {"id": c["id"], "result": out[:20000], "error": err, "note": ev.get("note")})
                if err and not blocked and tool_errors.get(c["name"], 0) < 2:
                    out += ("\n\n[Il tool ha restituito un errore. Se l'obiettivo NON è ancora stato raggiunto, "
                            "correggi gli argomenti e riprova UNA volta: usa solo valori ammessi e ometti i parametri "
                            "opzionali di cui non sei sicuro. Se un'altra chiamata in questo turno ha già avuto "
                            "successo, non riprovare: rispondi all'utente.]")
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        meta["error"] = explain_llm_error(e, model)
        yield sse("error", {"text": meta["error"]})
    finally:
        db.x("INSERT INTO messages(conv_id, role, content, meta, created_at) VALUES(?,?,?,?,?)",
             (cid, "assistant", full_text, json.dumps(meta, ensure_ascii=False), db.now()))
        db.x("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), cid))
    yield sse("done", {"conversation_id": cid})


@app.post("/api/chat")
async def chat(req: ChatIn):
    return StreamingResponse(chat_stream(req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


UI_DIR = os.path.join(db.DATA_DIR, "ui")
os.makedirs(UI_DIR, exist_ok=True)
_BG_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}


@app.post("/api/ui/background")
async def ui_background_upload(file: UploadFile = File(...)):
    if file.content_type not in _BG_TYPES:
        raise HTTPException(400, "Formato non supportato: usa JPG, PNG, WebP, GIF o AVIF")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "Immagine troppo grande (max 15 MB)")
    for f in os.listdir(UI_DIR):
        if f.startswith("background."):
            os.remove(os.path.join(UI_DIR, f))
    name = f"background.{_BG_TYPES[file.content_type]}"
    with open(os.path.join(UI_DIR, name), "wb") as fh:
        fh.write(data)
    import time as _t
    return {"url": f"/api/ui/background?v={int(_t.time())}"}


@app.get("/api/ui/background")
def ui_background():
    for f in os.listdir(UI_DIR):
        if f.startswith("background."):
            return FileResponse(os.path.join(UI_DIR, f), headers={"Cache-Control": "public, max-age=31536000"})
    raise HTTPException(404)


@app.delete("/api/ui/background")
def ui_background_delete():
    for f in os.listdir(UI_DIR):
        if f.startswith("background."):
            os.remove(os.path.join(UI_DIR, f))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "version": APP_VERSION}


# ======================= Frontend =======================
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _asset_tag() -> str:
    # cambia a ogni modifica dei file statici: il browser scarica sempre la versione aggiornata
    mt = max(os.path.getmtime(os.path.join(STATIC, f)) for f in ("app.js", "styles.css", "theme.js"))
    return f"{APP_VERSION}-{int(mt)}"


@app.get("/")
def index():
    from fastapi.responses import HTMLResponse
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    tag = _asset_tag()
    html = (html.replace("/static/app.js", f"/static/app.js?v={tag}")
                .replace("/static/theme.js", f"/static/theme.js?v={tag}")
                .replace("/static/styles.css", f"/static/styles.css?v={tag}")
                .replace("{{VERSION}}", APP_VERSION))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
