"""Risoluzione "intelligente" dei nomi delle entità (Home Assistant e server simili).

Gli intent di Home Assistant accettano solo il nome esatto (o un alias) di un'entità esposta. I modelli invece
usano spesso nomi approssimativi ("lampada del tavolo", "condizionatore soggiorno"). Questo modulo:
  1. costruisce un catalogo delle entità leggendo GetLiveContext (con cache);
  2. confronta il nome richiesto con il catalogo, combinando somiglianza lessicale e semantica (embedding);
  3. decide se correggere il nome, chiedere chiarimenti (ambiguità) o lasciarlo invariato.
"""
import difflib
import re
import time
import unicodedata

import numpy as np

from . import db

CACHE_TTL = 120  # secondi
_catalog: dict = {}   # server_id -> (timestamp, [entità])
_emb_cache: dict = {}  # testo -> vettore normalizzato

STOPWORDS = {
    "il", "lo", "la", "i", "gli", "le", "l", "del", "dello", "della", "dei", "degli", "delle",
    "di", "da", "in", "nel", "nello", "nella", "nei", "negli", "nelle", "su", "sul", "sulla", "al", "allo", "alla",
    "ai", "agli", "alle", "e", "per", "con", "mio", "mia", "the", "of", "my", "a", "an", "to", "on", "in",
}

# Sinonimi (italiano -> varianti italiane/inglesi): molti nomi delle entità sono in inglese o abbreviati
SYNONYMS = {
    "luce": ["light", "lampada", "lamp", "luci"], "luci": ["light", "luce", "lights"],
    "lampada": ["lamp", "light", "luce"], "lampadina": ["bulb", "light", "lampada"],
    "faro": ["floodlight", "spotlight", "flood"], "fari": ["floodlight", "spotlight"],
    "condizionatore": ["climate", "clima", "climatizzatore", "hvac"], "climatizzatore": ["climate", "clima"],
    "clima": ["climate", "condizionatore"], "riscaldamento": ["heating", "heater", "termostato", "thermostat"],
    "termostato": ["thermostat", "climate"], "ricarica": ["charge", "charging", "charger"], "carica": ["charge"],
    "auto": ["car", "automobile", "tesla"], "macchina": ["car", "automobile", "tesla"],
    "presa": ["socket", "outlet", "plug"], "spina": ["plug", "socket"],
    "temperatura": ["temperature", "temp"], "umidita": ["humidity"], "batteria": ["battery"],
    "consumo": ["energy", "consumption", "power"], "potenza": ["power"], "energia": ["energy"],
    "porta": ["door"], "cancello": ["gate"], "tapparella": ["shutter", "blind", "cover"],
    "serranda": ["shutter", "cover"], "tenda": ["curtain", "shade"], "televisore": ["tv"], "televisione": ["tv"],
    "esterno": ["outside", "outdoor", "esterna"], "esterna": ["outside", "outdoor", "esterno"],
    "interno": ["inside", "indoor"], "interna": ["inside", "indoor"], "sinistro": ["left"], "destro": ["right"],
    "anteriore": ["front"], "posteriore": ["rear", "back"], "pneumatico": ["tire", "tyre"], "gomma": ["tire"],
    "pressione": ["pressure"], "sedile": ["seat"], "volante": ["steering", "wheel"], "bagagliaio": ["trunk", "frunk"],
    "lavatrice": ["washer", "washing"], "lavastoviglie": ["dishwasher"], "asciugatrice": ["dryer"],
    "livello": ["level"], "stato": ["state", "status"], "ventilatore": ["fan"], "allarme": ["alarm"], "telecamera": ["camera", "cam"], "movimento": ["motion"],
    "salotto": ["soggiorno", "living"], "soggiorno": ["salotto", "living"], "cucina": ["kitchen"],
    "giardino": ["garden"], "ingresso": ["entrance", "entry"], "camera": ["bedroom", "room", "stanza"],
    "cameretta": ["bedroom", "bambini", "stanza"], "bagno": ["bathroom"], "studio": ["office"],
}
# Parole della richiesta che suggeriscono un dominio
DOMAIN_HINTS = {
    "luce": "light", "luci": "light", "lampada": "light", "lampadina": "light", "faro": "light", "fari": "light",
    "condizionatore": "climate", "clima": "climate", "climatizzatore": "climate", "termostato": "climate",
    "riscaldamento": "climate", "presa": "switch", "tapparella": "cover", "serranda": "cover", "tenda": "cover",
    "cancello": "cover", "sensore": "sensor", "temperatura": "sensor", "umidita": "sensor", "consumo": "sensor",
}

# Domini adatti a ciascun intent (bonus) e domini "sola lettura" (penalità per le azioni)
TOOL_DOMAINS = {
    "HassLightSet": {"light"},
    "HassClimateSetTemperature": {"climate"},
    "HassSetPosition": {"cover", "valve"},
    "HassTurnOn": {"light", "switch", "fan", "cover", "lock", "scene", "script", "input_boolean", "media_player",
                   "climate", "button", "valve", "humidifier", "vacuum"},
    "HassTurnOff": {"light", "switch", "fan", "cover", "lock", "input_boolean", "media_player", "climate", "valve",
                    "humidifier", "vacuum"},
    "HassMediaPause": {"media_player"}, "HassMediaUnpause": {"media_player"}, "HassMediaNext": {"media_player"},
    "HassMediaPrevious": {"media_player"}, "HassSetVolume": {"media_player"},
    "HassSetVolumeRelative": {"media_player"}, "HassMediaPlayerMute": {"media_player"},
    "HassMediaPlayerUnmute": {"media_player"},
}
READ_ONLY_DOMAINS = {"sensor", "binary_sensor", "weather", "conversation", "camera", "event", "sun", "zone",
                     "device_tracker", "person", "update"}


def base_tool(name: str) -> str:
    """Nome "nudo" del tool: Home Assistant espone i tool con un prefisso (intent__HassTurnOff,
    homeassistant__GetLiveContext, light__HassLightSet…)."""
    return str(name).split("__")[-1].split(".")[-1]


KNOWN_DOMAINS = {"light", "switch", "sensor", "binary_sensor", "climate", "cover", "fan", "lock", "media_player",
                 "scene", "script", "input_boolean", "button", "number", "valve", "vacuum", "humidifier", "camera",
                 "weather", "select", "input_number", "input_select", "automation", "siren", "water_heater"}


# ---------------------------------------------------------------- normalizzazione / punteggio lessicale
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)          # camelCase -> camel case
    s = re.sub(r"[_\-\.\[\]\(\)\|:/'’]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str):
    return [t for t in norm(s).split() if t not in STOPWORDS]


def _tok_match(a: str, b: str) -> float:
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short) and len(short) / len(long_) >= 0.5:
        return 0.9
    r = difflib.SequenceMatcher(None, a, b).ratio()
    return r if r >= 0.75 else 0.0


def _alts(t: str):
    return [t, *SYNONYMS.get(t, [])]


def lexical_score(query: str, candidate: str, extra: str = "") -> float:
    """Somiglianza tra la richiesta e il nome di un'entità (extra = dominio/area, contano meno)."""
    q = tokens(query)
    c_name = tokens(candidate)
    c = c_name + tokens(extra)
    if not q or not c_name:
        return 0.0
    joined = "".join(c_name)

    def tm(t):
        best = max(_tok_match(a, u) for a in _alts(t) for u in c)
        if len(t) >= 5 and t in joined:  # "lavastoviglie" dentro "lava-stoviglie-cantina"
            best = max(best, 0.9)
        return best

    cover = sum(tm(t) for t in q) / len(q)
    q_all = {a for t in q for a in _alts(t)}
    prec = sum(max(_tok_match(a, u) for a in q_all) for u in c_name) / len(c_name)
    seq = difflib.SequenceMatcher(None, "".join(q), joined).ratio()
    return 0.6 * cover + 0.15 * prec + 0.25 * seq


# ---------------------------------------------------------------- catalogo
def parse_live_context(text: str):
    """Estrae le entità dal testo YAML di GetLiveContext (formato originale, non compattato)."""
    if "- names:" not in text:
        return []
    _, _, body = text.partition("\n- names:")
    try:
        import yaml
        items = yaml.safe_load("- names:" + body)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in items or []:
        if not isinstance(e, dict) or not e.get("names"):
            continue
        names = [n.strip() for n in str(e["names"]).split(",") if n.strip()]
        attrs = e.get("attributes") or {}
        out.append({"name": names[0], "aliases": names, "domain": e.get("domain", ""),
                    "area": str(e.get("areas") or ""), "state": e.get("state"),
                    "device_class": attrs.get("device_class") if isinstance(attrs, dict) else None})
    return out


def _live_tool(server: dict):
    return next((t["name"] for t in server.get("tools", []) if base_tool(t["name"]) == "GetLiveContext"), None)


def server_supports(server: dict) -> bool:
    return _live_tool(server) is not None


async def get_catalog(server: dict, force: bool = False):
    from .mcp_client import _unwrap_json, open_session, _result_to_text  # import locale: evita cicli
    sid = server["id"]
    hit = _catalog.get(sid)
    if hit and not force and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        async with open_session(server) as s:
            res = await s.call_tool(_live_tool(server), {})
        ents = parse_live_context(_unwrap_json(_result_to_text(res)))
    except BaseException as e:  # noqa: BLE001
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        ents = hit[1] if hit else []
    _catalog[sid] = (time.time(), ents)
    return ents


def invalidate(server_id=None):
    if server_id is None:
        _catalog.clear()
    else:
        _catalog.pop(server_id, None)


# ---------------------------------------------------------------- embedding (facoltativo)
async def _embed_many(texts, settings):
    from .rag import embed
    missing = [t for t in dict.fromkeys(texts) if t not in _emb_cache]
    if missing:
        vecs = await embed(missing, settings)
        for t, v in zip(missing, vecs):
            _emb_cache[t] = v
    return np.vstack([_emb_cache[t] for t in texts])


def _ent_text(e):
    return f"{' / '.join(e['aliases'])} ({e['domain']}{', ' + e['area'] if e['area'] else ''})"


# ---------------------------------------------------------------- risoluzione
def _fix_filters(args: dict, ent: dict):
    """Dopo aver identificato l'entità, rimuove i filtri in conflitto (es. device_class='tv' per una luce),
    che farebbero fallire la chiamata in Home Assistant."""
    new, dropped = dict(args), []
    dc = new.get("device_class")
    if dc:
        dcs = dc if isinstance(dc, list) else [dc]
        if ent.get("device_class") not in dcs:
            new.pop("device_class")
            dropped.append(f"device_class={dcs}")
    dom = new.get("domain")
    if dom:
        doms = dom if isinstance(dom, list) else [dom]
        if ent["domain"] not in doms:
            new.pop("domain")
            dropped.append(f"domain={doms}")
    return new, dropped


def _mentioned_in(user_text: str, ent: dict) -> float:
    """Quanto l'entità è citata nel messaggio originale dell'utente (il modello a volte accorcia i nomi:
    "spegni la luce tv" -> name="tv")."""
    if not user_text:
        return 0.0
    ut = " " + " ".join(tokens(user_text)) + " "
    best = 0.0
    for a in ent["aliases"]:
        at = tokens(a)
        if not at:
            continue
        if len(at) >= 2 or len(at[0]) >= 4:
            if f" {' '.join(at)} " in ut:
                best = max(best, 1.0)  # alias citato per intero
                continue
        best = max(best, 0.5 * sum(1 for t in at if f" {t} " in ut) / len(at))
    return best


async def resolve(server: dict, tool_name: str, args: dict, settings: dict, user_text: str = ""):
    """Restituisce (nuovi_argomenti, nota, blocco).
    - nota: testo per l'interfaccia (nome corretto o diagnostica);
    - blocco: messaggio per il modello se il nome è ambiguo (in quel caso la chiamata NON va eseguita)."""
    tool_name = base_tool(tool_name)
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return args, None, None
    ents = await get_catalog(server)
    if not ents:
        return args, "Catalogo entità vuoto: impossibile interpretare il nome (GetLiveContext non ha risposto?)", None

    original = name
    # ID tecnico (es. "light.tavolo"): togli il dominio e usalo come indizio
    m = re.match(r"^\s*([a-z_]+)\.([a-z0-9_]+)\s*$", name)
    id_domain = None
    if m and m.group(1) in KNOWN_DOMAINS:
        id_domain, name = m.group(1), m.group(2)
        exact_id = [e for e in ents if e["domain"] == id_domain and any(norm(a) == norm(name) for a in e["aliases"])]
        if len(exact_id) == 1:
            new, dropped = _fix_filters(dict(args, name=exact_id[0]["name"]), exact_id[0])
            return new, f"ID «{original}» → nome «{exact_id[0]['name']}»", None

    # nome e alias copiati insieme ("lampada_tavolo_soggiorno Light, Tavolo"): prova ogni parte
    parts = [name] + ([p.strip() for p in name.split(",") if p.strip()] if "," in name else [])
    for part in parts:
        qn = norm(part)
        exact = [e for e in ents if any(norm(a) == qn for a in e["aliases"])]
        if exact:
            fixed, dropped = _fix_filters(args, exact[0]) if len(exact) == 1 else (args, [])
            if part != original:
                fixed = dict(fixed, name=exact[0]["name"] if len(exact) == 1 else part)
                return fixed, f"Nome corretto: «{original}» → «{fixed['name']}»" + (
                    f" · rimossi filtri incompatibili: {', '.join(dropped)}" if dropped else ""), None
            if dropped:
                return fixed, f"Nome esatto; rimossi i filtri incompatibili con «{exact[0]['name']}»: {', '.join(dropped)}", None
            return args, None, None

    is_action = tool_name != "GetLiveContext"
    wanted = TOOL_DOMAINS.get(tool_name, set())
    dom_arg = args.get("domain")
    dom_filter = set(dom_arg if isinstance(dom_arg, list) else [dom_arg]) - {None, ""} if dom_arg else set()
    if id_domain:
        dom_filter = {id_domain}
    area_arg = norm(args.get("area") or "")

    pool = [e for e in ents if not dom_filter or e["domain"] in dom_filter] or ents
    if is_action and wanted:
        pool = [e for e in pool if e["domain"] not in READ_ONLY_DOMAINS] or pool

    extra = lambda e: f"{e['domain']} {e['area']}"  # noqa: E731
    lex = np.array([max(lexical_score(name, a, extra(e)) for a in e["aliases"]) for e in pool])
    hinted = {DOMAIN_HINTS[t] for t in tokens(name) if t in DOMAIN_HINTS}
    sem = None
    if settings.get("entity_semantic", True):
        try:
            vecs = await _embed_many([_ent_text(e) for e in pool], settings)
            qv = (await _embed_many([name], settings))[0]
            sem = np.clip(vecs @ qv, 0, 1)
            # riporta il coseno su una scala comparabile al lessicale (gli embedding hanno coseni "compressi")
            lo, hi = float(sem.min()), float(sem.max())
            sem = (sem - lo) / (hi - lo) if hi - lo > 1e-6 else sem * 0
        except Exception:  # noqa: BLE001
            sem = None
    score = lex if sem is None else 0.65 * lex + 0.35 * sem * np.maximum(lex, 0.35)
    for i, e in enumerate(pool):
        if wanted and e["domain"] in wanted:
            score[i] += 0.06
        if hinted and e["domain"] in hinted:
            score[i] += 0.04
        if area_arg and e["area"] and (norm(e["area"]) == area_arg or lexical_score(area_arg, e["area"]) > 0.8):
            score[i] += 0.1
        if e.get("state") in ("unavailable",):
            score[i] -= 0.05
        m = _mentioned_in(user_text, e)
        if m:
            score[i] += 0.3 * m

    order = np.argsort(-score)
    best = pool[order[0]]
    b = float(score[order[0]])
    # il secondo candidato "diverso" (stesso nome = stessa entità esposta in più domini, es. presa + sensore)
    second = next((float(score[j]) for j in order[1:] if pool[j]["name"] != best["name"]), 0.0)
    thr = float(settings.get("entity_match_threshold") or 0.6)

    # nomi quasi identici (es. "Luce Cantina uno"/"due"): il solo dominio o l'area non bastano a decidere
    lex_second = next((float(lex[j]) for j in order[1:] if pool[j]["name"] != best["name"]), 0.0)
    near_twin = float(lex[order[0]]) - lex_second < 0.03 and lex_second >= thr
    if b >= thr and not near_twin and (b - second >= 0.07 or second < thr - 0.15):
        new, dropped = _fix_filters(args, best)
        new["name"] = best["name"]
        note = f"Entità interpretata: «{original}» → «{best['name']}» ({best['domain']}{', ' + best['area'] if best['area'] else ''}) · affidabilità {min(b, 1):.0%}"
        if dropped:
            note += f" · rimossi filtri incompatibili: {', '.join(dropped)}"
        return new, note, None

    cands = []
    for j in order[:6]:
        e = pool[j]
        if float(score[j]) < 0.3 or any(c.startswith(e["name"] + " ") for c in cands):
            continue
        cands.append(f"{e['name']} ({e['domain']}{', ' + e['area'] if e['area'] else ''})")
    if not cands:  # nessun candidato plausibile: lasciamo decidere al server
        return args, f"Nessuna entità simile a «{original}» tra le {len(ents)} del catalogo", None
    msg = (f"Nessuna entità si chiama esattamente «{original}». Candidati più simili: " + "; ".join(cands) + ". "
           "Se uno di questi corrisponde chiaramente alla richiesta dell'utente richiama lo strumento con quel nome "
           "esatto; se più di uno è plausibile, CHIEDI all'utente quale intende, senza eseguire l'azione.")
    return args, f"Nome ambiguo «{original}»: chiesti chiarimenti al modello", msg
