"""Client MCP: connessione a server Streamable HTTP, SSE o stdio, elenco e invocazione dei tool."""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from . import db

CONNECT_TIMEOUT = float(os.environ.get("MCP_CONNECT_TIMEOUT", "30"))
CALL_TIMEOUT = float(os.environ.get("MCP_CALL_TIMEOUT", "120"))
# i server stdio avviati con uvx/npx scaricano i pacchetti alla prima esecuzione: serve più tempo
STDIO_TIMEOUT = float(os.environ.get("MCP_STDIO_TIMEOUT", "240"))


@asynccontextmanager
async def open_session(server: dict):
    transport = server.get("transport", "http")
    headers = {k: v for k, v in (server.get("headers") or {}).items() if k}
    token = (server.get("auth_token") or "").strip()
    if token:
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        # il token dedicato prevale su un eventuale header Authorization manuale
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
        headers["Authorization"] = f"Bearer {token}"
    if transport == "http":
        async with create_mcp_http_client(headers=headers or None) as http:
            async with streamable_http_client(server["url"], http_client=http) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as s:
                    s.nexus_init = await asyncio.wait_for(s.initialize(), CONNECT_TIMEOUT)
                    yield s
    elif transport == "sse":
        async with sse_client(server["url"], headers=headers or None) as (read, write):
            async with ClientSession(read, write) as s:
                s.nexus_init = await asyncio.wait_for(s.initialize(), CONNECT_TIMEOUT)
                yield s
    elif transport == "stdio":
        params = StdioServerParameters(
            command=server["command"],
            args=server.get("args") or [],
            env={**os.environ, **(server.get("env") or {})},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                s.nexus_init = await asyncio.wait_for(s.initialize(), STDIO_TIMEOUT)
                yield s
    else:
        raise ValueError(f"Trasporto non supportato: {transport}")


def _attr(obj, *names):
    """Compatibilità tra versioni dell'SDK (snake_case in 2.x, camelCase in 1.x)."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _err_text(e: BaseException) -> str:
    # anyio può avvolgere gli errori in ExceptionGroup: estraiamo il primo messaggio utile
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


async def _http_hint(server: dict) -> str:
    """Per i server remoti prova a capire se l'errore è di autenticazione, per un messaggio più chiaro."""
    if server.get("transport") not in ("http", "sse") or not server.get("url"):
        return ""
    try:
        import httpx
        headers = {k: v for k, v in (server.get("headers") or {}).items() if k}
        if server.get("auth_token"):
            tok = server["auth_token"].strip()
            tok = tok[7:].strip() if tok.lower().startswith("bearer ") else tok
            headers["Authorization"] = f"Bearer {tok}"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(server["url"], headers={**headers, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 0, "method": "ping"})
        if r.status_code == 401:
            return " — HTTP 401: token mancante o non valido" if server.get("auth_token") else \
                " — HTTP 401: il server richiede autenticazione (imposta il Bearer token)"
        if r.status_code == 403:
            return " — HTTP 403: accesso negato con il token fornito"
        if r.status_code == 404:
            return " — HTTP 404: URL errato (controlla il path, es. /mcp)"
    except Exception:  # noqa: BLE001
        pass
    return ""


async def refresh_server(sid: int) -> dict:
    """Si collega al server, legge i tool e aggiorna il DB."""
    server = db.get_server(sid)
    try:
        async with open_session(server) as s:
            instructions = getattr(getattr(s, "nexus_init", None), "instructions", None) or ""
            res = await asyncio.wait_for(s.list_tools(), STDIO_TIMEOUT if server.get("transport") == "stdio" else CONNECT_TIMEOUT)
            tools = [{
                "name": t.name,
                "description": t.description or "",
                "input_schema": _attr(t, "input_schema", "inputSchema") or {"type": "object", "properties": {}},
            } for t in res.tools]
        db.sync_tools(sid, tools)
        from . import entities
        entities.invalidate(sid)
        db.x("UPDATE mcp_servers SET status='online', last_error='', instructions=? WHERE id=?", (instructions, sid))
        return {"ok": True, "count": len(tools)}
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        msg = _err_text(e) + await _http_hint(server)
        db.x("UPDATE mcp_servers SET status='error', last_error=? WHERE id=?", (msg, sid))
        return {"ok": False, "error": msg}


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}


def normalize_schema(schema, depth=0):
    """Semplifica gli schemi JSON per i backend locali (Ollama, llama.cpp), che gestiscono male
    anyOf/oneOf/$ref: per ogni proprietà sceglie un solo tipo, preferendo quello più semplice."""
    if not isinstance(schema, dict) or depth > 8:
        return {"type": "string"}
    out = {k: v for k, v in schema.items() if k not in ("anyOf", "oneOf", "allOf", "$schema", "$defs",
                                                        "definitions", "$ref", "additionalProperties")}
    variants = schema.get("anyOf") or schema.get("oneOf")
    if variants and "type" not in schema:
        simple = [v for v in variants if isinstance(v, dict) and v.get("type") not in (None, "null")]
        pick = next((v for v in simple if v.get("type") in ("string", "number", "integer", "boolean")), None) \
            or (simple[0] if simple else {"type": "string"})
        merged = normalize_schema(pick, depth + 1)
        merged.update({k: v for k, v in out.items() if k not in ("type", "items", "properties")})
        out = merged
    if schema.get("allOf") and "type" not in out:
        for part in schema["allOf"]:
            if isinstance(part, dict):
                out.update(normalize_schema(part, depth + 1))
    if out.get("default", 0) is None:
        out.pop("default")
    t = out.get("type")
    if isinstance(t, list):  # es. ["string", "null"]
        t = next((x for x in t if x != "null"), "string")
        out["type"] = t
    if t not in _JSON_TYPES:
        out["type"] = "object" if "properties" in out else "string"
    if out["type"] == "object":
        props = out.get("properties") or {}
        out["properties"] = {k: normalize_schema(v, depth + 1) for k, v in props.items()}
        if "required" in out:
            out["required"] = [r for r in out["required"] if r in out["properties"]]
    if out["type"] == "array":
        out["items"] = normalize_schema(out.get("items") or {"type": "string"}, depth + 1)
    return out


def build_openai_tools():
    """Restituisce (tools_openai, mappa nome_funzione -> (server, nome_tool), istruzioni dei server)."""
    tools, mapping, instructions = [], {}, []
    for srv in db.list_servers():
        if not srv["enabled"]:
            continue
        prefix = _safe_name(srv["name"])[:20] or f"srv{srv['id']}"
        for t in srv["tools"]:
            if not t["enabled"]:
                continue
            fname = f"{prefix}__{_safe_name(t['name'])}"[:64]
            if fname in mapping:
                fname = f"s{srv['id']}__{_safe_name(t['name'])}"[:64]
            schema = normalize_schema(t["input_schema"] or {"type": "object", "properties": {}})
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            tools.append({
                "type": "function",
                "function": {
                    "name": fname,
                    "description": (t["description"] or t["name"])[:1024],
                    "parameters": schema,
                },
            })
            mapping[fname] = (srv, t["name"])
        if srv.get("instructions") and any(t["enabled"] for t in srv["tools"]):
            instructions.append((srv["name"], srv["instructions"]))
    return tools, mapping, instructions


def _result_to_text(res) -> str:
    parts = []
    for c in getattr(res, "content", None) or []:
        ctype = getattr(c, "type", "")
        if ctype == "text":
            parts.append(c.text)
        elif ctype == "image":
            parts.append(f"[immagine {_attr(c, 'mime_type', 'mimeType') or ''}]")
        elif ctype == "resource":
            r = c.resource
            parts.append(getattr(r, "text", None) or f"[risorsa {getattr(r, 'uri', '')}]")
        else:
            try:
                parts.append(json.dumps(c.model_dump(mode="json"), ensure_ascii=False))
            except Exception:
                parts.append(str(c))
    structured = _attr(res, "structured_content", "structuredContent")
    if structured and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False))
    return "\n".join(parts) if parts else "(nessun output)"


# ---------------- Pulizia argomenti ----------------
_WILDCARDS = {"all", "*", "any", "every", "everything", "none", "null", "tutti", "tutte", "tutto", "qualsiasi", ""}


def _enum_of(prop: dict):
    if prop.get("enum"):
        return prop["enum"]
    items = prop.get("items") or {}
    if isinstance(items, dict) and items.get("enum"):
        return items["enum"]
    for v in prop.get("anyOf") or []:
        if isinstance(v, dict):
            e = _enum_of(v)
            if e:
                return e
    return None


def clean_args(args: dict, schema: dict):
    """Pulisce i parametri OPZIONALI prima della chiamata:
    - rimuove i valori "jolly" (es. domain="all") usati dai modelli per dire "nessun filtro";
    - rimuove i valori fuori dall'elenco ammesso (es. device_class="light" quando non è previsto).
    Restituisce (argomenti, note)."""
    if not isinstance(args, dict):
        return {}, []
    required = set((schema or {}).get("required") or [])
    props = (schema or {}).get("properties") or {}
    out, notes = {}, []
    for k, v in args.items():
        if k in required:
            out[k] = v
            continue
        enum = _enum_of(props.get(k) or {}) or []
        vals = v if isinstance(v, list) else [v]
        if v is None or v == [] or (vals and all(isinstance(x, str) and x.strip().lower() in _WILDCARDS
                                                 and x not in enum for x in vals)):
            notes.append(f"{k} (valore jolly)")
            continue
        if enum:
            good = [x for x in vals if x in enum]
            if len(good) < len(vals):
                bad = [x for x in vals if x not in enum]
                notes.append(f"{k}={bad} (non ammesso)")
                if not good:
                    continue
                v = good if isinstance(v, list) else good[0]
        out[k] = v
    return out, notes


_TARGET_KEYS = ("name", "area", "floor", "entity_id", "device_id", "area_id")


def broad_action_problem(tool_name: str, args: dict, schema: dict):
    """Per gli intent di Home Assistant (Hass*) che agiscono su entità: blocca le chiamate senza destinatario,
    che verrebbero eseguite su TUTTE le entità del tipo indicato (es. spegnere tutte le prese)."""
    props = (schema or {}).get("properties") or {}
    base = str(tool_name).split("__")[-1]  # Home Assistant: intent__HassTurnOff, light__HassLightSet…
    if not base.startswith("Hass") or not any(k in props for k in ("name", "area", "floor")):
        return None
    if any(args.get(k) for k in _TARGET_KEYS):
        return None
    return ("Azione BLOCCATA per sicurezza: hai chiamato un'azione senza indicare 'name' o 'area', quindi verrebbe "
            "eseguita su TUTTE le entità di quel tipo. Indica il nome esatto dell'entità (es. name='Luce Portone'). "
            "Se l'azione precedente con il nome era già riuscita, NON ripeterla: rispondi all'utente.")


def tool_schema(server_id: int, tool_name: str) -> dict:
    for t in db.list_tools(server_id):
        if t["name"] == tool_name:
            return t["input_schema"] or {}
    return {}


# ---------------- Post-elaborazione dei risultati ----------------
def _unwrap_json(text: str) -> str:
    """Molti server (es. Home Assistant) restituiscono {"result": "<testo>"} con escape JSON
    (\\n, \\u00b0…): estrae il testo, che è più corto e più leggibile per il modello."""
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        return text
    try:
        data = json.loads(t)
    except Exception:  # noqa: BLE001
        return text
    if isinstance(data, dict) and len(data) <= 2 and isinstance(data.get("result"), str):
        return data["result"]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) if isinstance(data, (dict, list)) else text


def _fmt_val(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def compact_live_context(text: str) -> str:
    """Compatta il "Live Context" di Home Assistant (YAML, ~8 righe per entità) in una riga per entità
    e raggruppa le entità non disponibili. Restituisce il testo originale se il formato non corrisponde."""
    if "- names:" not in text:
        return text
    head, sep, body = text.partition("\n- names:")
    if not sep:
        return text
    try:
        import yaml
        items = yaml.safe_load("- names:" + body)
    except Exception:  # noqa: BLE001
        return text
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return text
    lines, unavailable = [], []
    for e in items:
        names = [n.strip() for n in str(e.get("names", "?")).split(",") if n.strip()]
        name = names[0] + (f" (alias: {', '.join(names[1:])})" if len(names) > 1 else "")
        domain = e.get("domain", "")
        state = e.get("state")
        area = e.get("areas")
        if state in ("unavailable", "unknown"):
            unavailable.append(f"{name} ({domain}{', ' + str(area) if area else ''})")
            continue
        attrs = {k: v for k, v in (e.get("attributes") or {}).items() if v not in (None, "", [])}
        unit = attrs.pop("unit_of_measurement", "")
        if "brightness_pct" in attrs:
            attrs.pop("brightness", None)
        if unit or domain not in ("binary_sensor", "cover", "switch"):
            attrs.pop("device_class", None)
        val = f"{_fmt_val(state)}{(' ' + unit) if unit else ''}"
        extra = ", ".join(f"{k}={_fmt_val(v)}" for k, v in attrs.items())
        line = f"- {name} [{domain}{' · ' + str(area) if area else ''}]: {val}" + (f" ({extra})" if extra else "")
        lines.append(line)
    out = [head.strip() or "Live Context:",
           f"Formato: nome (alias) [dominio · area]: stato (attributi). Entità totali: {len(items)}. "
           "Per agire su un'entità usa come 'name' il suo nome OPPURE uno dei suoi alias, esattamente come scritto "
           "(non l'ID tecnico tipo light.xxx e non nome+alias insieme).", *lines]
    if unavailable:
        out.append(f"Non disponibili ({len(unavailable)}): " + "; ".join(unavailable))
    return "\n".join(out)


def postprocess_result(text: str, compact: bool = True) -> str:
    text = _unwrap_json(text)
    if compact:
        text = compact_live_context(text)
    return text


async def call_tool(server: dict, tool_name: str, args: dict):
    """Esegue un tool; restituisce (testo_risultato, is_error)."""
    try:
        async with open_session(server) as s:
            res = await asyncio.wait_for(s.call_tool(tool_name, args), CALL_TIMEOUT)
        compact = bool(db.get_settings().get("compact_tool_results", True))
        return postprocess_result(_result_to_text(res), compact), bool(_attr(res, "is_error", "isError"))
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        return f"Errore durante l'esecuzione del tool: {_err_text(e)}", True



def parse_mcp_config(cfg):
    """Converte una configurazione MCP in stile Claude Desktop/Cursor/VS Code in server Nexus.
    Accetta {"mcpServers": {...}}, {"servers": {...}} o direttamente {"nome": {...}}."""
    if isinstance(cfg, str):
        txt = cfg.strip()
        if txt.startswith('"'):          # frammento senza graffe esterne: "nome": {...}
            txt = "{" + txt + "}"
        try:
            cfg = json.loads(txt)
        except json.JSONDecodeError:
            # tollera graffe mancanti alla fine (copia/incolla parziale)
            for extra in ("}", "}}", "}}}"):
                try:
                    cfg = json.loads(txt + extra)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                raise ValueError("JSON non valido")
    if not isinstance(cfg, dict):
        raise ValueError("La configurazione deve essere un oggetto JSON")
    servers = cfg.get("mcpServers") or cfg.get("servers") or cfg.get("mcp", {}).get("servers") or cfg
    out = []
    for name, sc in servers.items():
        if not isinstance(sc, dict):
            continue
        headers = dict(sc.get("headers") or {})
        token = ""
        for k in list(headers):
            if k.lower() == "authorization" and str(headers[k]).lower().startswith("bearer "):
                token = str(headers.pop(k))[7:].strip()
        if sc.get("command"):
            out.append({"name": name, "transport": "stdio", "url": "", "headers": {}, "auth_token": "",
                        "command": sc["command"], "args": [str(a) for a in sc.get("args") or []],
                        "env": {k: str(v) for k, v in (sc.get("env") or {}).items()}})
        elif sc.get("url") or sc.get("serverUrl"):
            typ = str(sc.get("type") or sc.get("transport") or "").lower()
            url = sc.get("url") or sc.get("serverUrl")
            transport = "sse" if typ == "sse" or (not typ and url.rstrip("/").endswith("/sse")) else "http"
            out.append({"name": name, "transport": transport, "url": url, "headers": headers, "auth_token": token,
                        "command": "", "args": [], "env": {}})
        else:
            raise ValueError(f"Server «{name}»: manca 'command' (stdio) o 'url' (remoto)")
    if not out:
        raise ValueError("Nessun server trovato nella configurazione")
    return out
