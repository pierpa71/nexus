"""Client per endpoint OpenAI-compatibili (Ollama, vLLM, LM Studio, OpenAI, LiteLLM, ...)."""
from openai import AsyncOpenAI


def chat_client(settings) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=settings["llm_base_url"].rstrip("/"),
                       api_key=settings.get("llm_api_key") or "none", timeout=600)


def embed_client(settings) -> AsyncOpenAI:
    base = (settings.get("embed_base_url") or settings["llm_base_url"]).rstrip("/")
    key = settings.get("embed_api_key") or settings.get("llm_api_key") or "none"
    return AsyncOpenAI(base_url=base, api_key=key, timeout=300)


# ---------------- Diagnostica Ollama ----------------
import re

import httpx

_is_ollama: dict = {}


def _root(base_url: str) -> str:
    return re.sub(r"/v1/?$", "", base_url.rstrip("/"))


async def ollama_runtime(settings, model: str) -> dict:
    """Se l'endpoint è Ollama restituisce {"ollama": True, "ctx": <num_ctx effettivo o None>}.
    Il valore viene letto da /api/ps (solo a modello caricato in memoria)."""
    root = _root(settings["llm_base_url"])
    if _is_ollama.get(root) is False:
        return {"ollama": False, "ctx": None}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(root + "/api/ps")
        data = r.json() if r.status_code == 200 else None
        if not isinstance(data, dict) or "models" not in data:
            _is_ollama[root] = False
            return {"ollama": False, "ctx": None}
        _is_ollama[root] = True
        for m in data.get("models") or []:
            if model in (m.get("name"), m.get("model")) or m.get("name", "").split(":")[0] == model:
                return {"ollama": True, "ctx": m.get("context_length")}
        return {"ollama": True, "ctx": None}
    except Exception:  # noqa: BLE001
        _is_ollama[root] = False
        return {"ollama": False, "ctx": None}
