"""OCR con Tesseract per PDF scansionati e immagini.

Le pagine PDF vengono rese come immagini con pypdfium2 e riconosciute in parallelo,
con tutte le lingue abilitate (es. ita+eng)."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

log = logging.getLogger("nexus.ocr")

TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "tesseract")
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))
OCR_AUTOROTATE = os.environ.get("OCR_AUTOROTATE", "true").lower() in ("1", "true", "yes")
OCR_PAGE_TIMEOUT = int(os.environ.get("OCR_PAGE_TIMEOUT", "180"))  # secondi

# lingua PostgreSQL -> codice Tesseract
TESS_CODES = {"italian": "ita", "english": "eng", "french": "fra", "german": "deu", "spanish": "spa"}
# pdfium non è thread-safe, nemmeno tra documenti diversi: apertura e rendering sono serializzati
PDFIUM_LOCK = threading.Lock()
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}


@lru_cache(maxsize=1)
def installed() -> bool:
    if not shutil.which(TESSERACT_CMD):
        log.warning("Tesseract non trovato (%s): OCR non disponibile", TESSERACT_CMD)
        return False
    return True


def available(mode: str) -> bool:
    return mode != "off" and installed()


@lru_cache(maxsize=8)
def _languages(wanted: str) -> str:
    """Codici Tesseract da usare, limitati a quelli effettivamente installati."""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    have = set(pytesseract.get_languages(config=""))
    usable = [c for c in wanted.split("+") if c in have]
    missing = [c for c in wanted.split("+") if c not in have]
    if missing:
        log.warning("Pacchetti lingua Tesseract mancanti: %s", ", ".join(missing))
    return "+".join(usable) or "eng"


def languages(kb_languages) -> str:
    wanted = os.environ.get("OCR_LANGS") or "+".join(TESS_CODES[lg] for lg in kb_languages if lg in TESS_CODES)
    return _languages(wanted or "eng")


def _prepare(img):
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img)  # foto da smartphone con orientamento nei metadati EXIF
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    img = img.convert("L")
    # le immagini piccole (screenshot, foto a bassa risoluzione) si riconoscono meglio ingrandite
    if max(img.size) < 1600:
        factor = 1600 / max(img.size)
        img = img.resize((int(img.width * factor), int(img.height * factor)))
    return img


def _rotate_if_needed(img):
    """Raddrizza le pagine scansionate ruotate di 90/180/270 gradi."""
    if not OCR_AUTOROTATE:
        return img
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(img, config="--psm 0", timeout=30)
        angle = int(re.search(r"Rotate: (\d+)", osd).group(1))
        conf = float(re.search(r"Orientation confidence: ([\d.]+)", osd).group(1))
        if angle and conf >= 2.0:
            return img.rotate(-angle, expand=True)
    except Exception:  # noqa: BLE001 — pagine con poco testo: OSD non riesce a decidere
        pass
    return img


def image_to_text(img, langs: str) -> str:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    img = _rotate_if_needed(_prepare(img))
    text = pytesseract.image_to_string(img, lang=langs, config="--oem 1 --psm 3", timeout=OCR_PAGE_TIMEOUT)
    return _clean(text)


def _clean(text: str) -> str:
    text = text.replace("\x0c", "")
    # righe composte solo da rumore (bordi, puntini, timbri)
    lines = [l for l in text.splitlines() if not l.strip() or re.search(r"[A-Za-zÀ-ÿ0-9]{2,}", l)]
    return "\n".join(lines).strip()


def _safe_ocr(args) -> str:
    img, langs = args
    try:
        return image_to_text(img, langs)
    except Exception as e:  # noqa: BLE001
        log.warning("OCR fallito su una pagina: %s", e)
        return ""


def pdf_pages(data: bytes, page_numbers: list[int], langs: str, workers: int = 2) -> dict[int, str]:
    """OCR delle pagine indicate (numerate da 1). Rende a blocchi per contenere la memoria."""
    import pypdfium2 as pdfium

    workers = max(1, int(workers))
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(data)
    scale = OCR_DPI / 72
    results: dict[int, str] = {}
    batch = workers * 2
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(page_numbers), batch):
                chunk = page_numbers[start:start + batch]
                # pdfium non è thread-safe: il rendering resta sequenziale, l'OCR va in parallelo
                with PDFIUM_LOCK:
                    images = [pdf[n - 1].render(scale=scale, grayscale=True).to_pil() for n in chunk]
                for n, text in zip(chunk, pool.map(_safe_ocr, [(im, langs) for im in images])):
                    results[n] = text
                log.info("OCR: %d/%d pagine", min(start + batch, len(page_numbers)), len(page_numbers))
    finally:
        with PDFIUM_LOCK:
            pdf.close()
    return results


def image_pages(data: bytes, langs: str, workers: int = 2) -> list[tuple[int, str]]:
    """OCR di un'immagine; i TIFF multipagina (tipici degli scanner) restituiscono una voce per pagina."""
    from PIL import Image, ImageSequence

    img = Image.open(io.BytesIO(data))
    frames = [f.copy() for f in ImageSequence.Iterator(img)]
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        texts = list(pool.map(_safe_ocr, [(f, langs) for f in frames]))
    return [(i, t) for i, t in enumerate(texts, start=1) if t.strip()]
