"""Immagini allegate ai messaggi della chat, inviate ai modelli vision (formato OpenAI image_url).

Per ogni immagine si salvano due file in DATA_DIR/chat_images:
  <id>.<ext>      l'originale, mostrato nella chat (le GIF restano animate)
  <id>.llm.<ext>  la versione per il modello: orientamento EXIF applicato, primo fotogramma per le
                  animazioni, lato lungo ridotto a MAX_SIDE, JPEG (o PNG se c'è trasparenza)
"""
import base64
import io
import os
import re
import uuid

from . import db

IMG_DIR = os.path.join(db.DATA_DIR, "chat_images")
os.makedirs(IMG_DIR, exist_ok=True)

MAX_BYTES = 20 * 1024 * 1024
MAX_SIDE = 1536          # oltre questa dimensione i modelli vision ridimensionano comunque
TOKENS_PER_IMAGE = 1000  # stima prudente del costo in contesto di un'immagine (dipende dal modello)
_ID = re.compile(r"^[0-9a-f]{32}$")
_EXT = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tif", "AVIF": "avif"}
MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        "bmp": "image/bmp", "tif": "image/tiff", "avif": "image/avif"}


def _find(iid: str, llm: bool) -> str | None:
    if not _ID.match(iid or ""):
        return None
    for f in os.listdir(IMG_DIR):
        if f.startswith(iid + ".") and (".llm." in f) == llm:
            return os.path.join(IMG_DIR, f)
    return None


def save(data: bytes, name: str) -> dict:
    """Valida e salva un'immagine. Solleva ValueError con un messaggio leggibile."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    if len(data) > MAX_BYTES:
        raise ValueError(f"«{name}» è troppo grande (max {MAX_BYTES // 1024 // 1024} MB)")
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"«{name}» non è un'immagine leggibile (formati: JPG, PNG, GIF, WebP, BMP, TIFF, AVIF)") from e
    ext = _EXT.get(fmt or "")
    if not ext:
        raise ValueError(f"«{name}»: formato {fmt or 'sconosciuto'} non supportato")
    iid = uuid.uuid4().hex
    with open(os.path.join(IMG_DIR, f"{iid}.{ext}"), "wb") as fh:
        fh.write(data)

    img.seek(0)  # GIF/WebP animati: primo fotogramma
    img = ImageOps.exif_transpose(img)
    alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if alpha else "RGB")
    w, h = img.size
    if max(w, h) > MAX_SIDE:
        k = MAX_SIDE / max(w, h)
        img = img.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    out = io.BytesIO()
    if alpha:
        img.save(out, "PNG", optimize=True)
        llm_ext = "png"
    else:
        img.save(out, "JPEG", quality=88)
        llm_ext = "jpg"
    with open(os.path.join(IMG_DIR, f"{iid}.llm.{llm_ext}"), "wb") as fh:
        fh.write(out.getvalue())
    return {"id": iid, "name": name, "mime": MIME[ext], "width": w, "height": h, "url": f"/api/chat/images/{iid}"}


def original_path(iid: str) -> str | None:
    return _find(iid, llm=False)


def data_url(iid: str) -> str | None:
    p = _find(iid, llm=True)
    if not p:
        return None
    with open(p, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return f"data:{MIME[p.rsplit('.', 1)[1]]};base64,{b64}"


def content_parts(text: str, ids: list[str]) -> str | list:
    """Contenuto di un messaggio utente: stringa semplice o lista text + image_url (formato OpenAI)."""
    urls = [u for u in (data_url(i) for i in ids or []) if u]
    if not urls:
        return text
    return [{"type": "text", "text": text}, *({"type": "image_url", "image_url": {"url": u}} for u in urls)]


def delete(ids) -> None:
    for iid in ids or []:
        for llm in (False, True):
            p = _find(iid, llm)
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def strip_for_estimate(obj):
    """Copia dell'oggetto con le immagini sostituite da un segnaposto, e quante erano.
    Serve alle stime dei token: il base64 non riflette il costo reale di un'immagine."""
    count = 0

    def walk(o):
        nonlocal count
        if isinstance(o, dict):
            if o.get("type") == "image_url":
                count += 1
                return {"type": "image"}
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return walk(obj), count
