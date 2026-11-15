"""Estrazione del testo dai documenti (con OCR dove serve) e suddivisione in chunk con il numero di pagina."""
from __future__ import annotations

import io
import re
from pathlib import Path

from . import ocr

DOC_EXT = {".pdf", ".docx", ".html", ".htm"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".rst", ".yaml", ".yml", ".log", ".ini",
            ".toml", ".py", ".js", ".ts", ".java", ".go", ".sql", ".sh", ".c", ".h", ".cpp", ".cs", ".rb",
            ".php", ".rs", ".kt", ".swift", ".css", ".tex"}
SUPPORTED = DOC_EXT | TEXT_EXT | ocr.IMAGE_EXT


class ExtractOptions:
    def __init__(self, languages, ocr_mode="auto", ocr_workers=2, ocr_min_chars=40):
        self.languages = languages
        self.ocr_mode = ocr_mode
        self.ocr_workers = ocr_workers
        self.ocr_min_chars = ocr_min_chars


def extract(filename: str, data: bytes, opt: ExtractOptions) -> tuple[list[tuple[int | None, str]], dict]:
    """Restituisce ([(pagina, testo), ...], info). Pagina è None per i formati senza pagine.
    info contiene "ocr_pages": le pagine il cui testo è stato ottenuto con l'OCR."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _pdf(data, opt)
    if ext in ocr.IMAGE_EXT:
        if not ocr.available(opt.ocr_mode):
            raise ValueError("Per le immagini serve l'OCR, che non è attivo (Tesseract assente o OCR disattivato "
                             "nelle impostazioni).")
        pages = ocr.image_pages(data, ocr.languages(opt.languages), opt.ocr_workers)
        if not pages:
            raise ValueError("L'OCR non ha trovato testo leggibile nell'immagine.")
        return pages, {"ocr_pages": [p for p, _ in pages]}
    if ext == ".docx":
        return [(None, _docx(data))], {}
    if ext in {".html", ".htm"}:
        return [(None, _html(data))], {}
    if ext in TEXT_EXT or not _looks_binary(data):
        return [(None, _decode(data))], {}
    raise ValueError(f"Formato non supportato: {ext or 'senza estensione'}. "
                     "Ammessi: PDF, DOCX, immagini (OCR), HTML e file di testo o codice.")


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdfium_texts(data: bytes) -> dict[int, str]:
    """Testo delle pagine con pdfium, senza le cifre in apice (richiami di nota): altrimenti "Art. 56⁹"
    diventerebbe "Art. 569" e né la ricerca né il modello troverebbero l'articolo 56."""
    import statistics

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    texts: dict[int, str] = {}
    with ocr.PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(data)
        try:
            for i in range(len(pdf)):
                try:
                    tp = pdf[i].get_textpage()
                    n = tp.count_chars()
                    full = tp.get_text_range() if n else ""
                    sizes = [raw.FPDFText_GetFontSize(tp.raw, j) for j in range(min(n, len(full)))]
                    body = statistics.median([sz for sz in sizes if sz > 2] or [0])
                    if body:
                        full = "".join(ch for j, ch in enumerate(full)
                                       if not (j < len(sizes) and ch.isdigit() and 0 < sizes[j] < body * 0.75))
                    texts[i + 1] = full
                except Exception:  # noqa: BLE001 — una pagina illeggibile non blocca il documento
                    texts[i + 1] = ""
        finally:
            pdf.close()
    return texts


def _pypdf_texts(data: bytes) -> dict[int, str]:
    from pypdf import PdfReader

    texts: dict[int, str] = {}
    for i, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
        try:
            texts[i] = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            texts[i] = ""
    return texts


def _pdf(data: bytes, opt: ExtractOptions):
    try:
        texts = _pdfium_texts(data)
    except Exception:  # noqa: BLE001 — PDF che pdfium non apre: si prova con pypdf
        texts = {}
    # pagine senza testo per pdfium: tentativo con pypdf prima di passare all'OCR
    if not texts or any(len(re.sub(r"\s", "", t)) < opt.ocr_min_chars for t in texts.values()):
        try:
            alt = _pypdf_texts(data)
        except Exception:  # noqa: BLE001
            alt = {}
        for i, t in alt.items():
            if len(t.strip()) > len(texts.get(i, "").strip()):
                texts[i] = t
    if not texts:
        raise ValueError("Impossibile leggere il PDF.")

    # pagine da passare all'OCR: tutte (always) o solo quelle senza testo utile (auto).
    # Gestisce anche i PDF misti: es. relazione digitale con allegati firmati e scansionati.
    ocr_pages: list[int] = []
    if ocr.available(opt.ocr_mode):
        if opt.ocr_mode == "always":
            ocr_pages = list(texts)
        else:
            ocr_pages = [i for i, t in texts.items() if len(re.sub(r"\s", "", t)) < opt.ocr_min_chars]
        if ocr_pages:
            for i, text in ocr.pdf_pages(data, ocr_pages, ocr.languages(opt.languages), opt.ocr_workers).items():
                # se l'OCR produce meno testo dell'estrazione nativa, si tiene quella nativa
                if len(text.strip()) >= len(texts[i].strip()):
                    texts[i] = text
                else:
                    ocr_pages.remove(i)

    pages = [(i, t) for i, t in texts.items() if t.strip()]
    if not pages:
        hint = ("l'OCR non ha trovato testo leggibile" if ocr.available(opt.ocr_mode)
                else "probabilmente è una scansione: l'OCR non è attivo, verifica Tesseract e la modalità OCR")
        raise ValueError(f"Il PDF non contiene testo estraibile ({hint}).")
    return pages, {"ocr_pages": [i for i in ocr_pages if texts[i].strip()]}


def _docx(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = []
    # Paragrafi e tabelle nell'ordine in cui compaiono nel documento
    for block in d.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(t.text or "" for t in block.iter() if t.tag.endswith("}t"))
            if text.strip():
                parts.append(text)
        elif tag == "tbl":
            for row in block.iter():
                if row.tag.endswith("}tr"):
                    cells = []
                    for cell in row.iterchildren():
                        if cell.tag.endswith("}tc"):
                            cells.append(" ".join(t.text or "" for t in cell.iter() if t.tag.endswith("}t")).strip())
                    if any(cells):
                        parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def _html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text("\n")


# --- Chunking -----------------------------------------------------------------

def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"-\n(\w)", r"\1", text)          # parole spezzate a fine riga
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")


def _pieces(text: str, size: int) -> list[str]:
    """Spezza il testo in unità (paragrafi, poi frasi, poi parole) non più lunghe di `size`."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(para)
            continue
        for sent in _SENTENCE.split(para):
            if len(sent) <= size:
                out.append(sent)
            else:
                buf = ""
                for w in sent.split():
                    if len(buf) + len(w) + 1 > size and buf:
                        out.append(buf)
                        buf = ""
                    buf = f"{buf} {w}".strip()
                if buf:
                    out.append(buf)
    return out


def chunk(pages: list[tuple[int | None, str]], size: int, overlap: int) -> list[tuple[int | None, str]]:
    """Unisce le unità in chunk di circa `size` caratteri con `overlap` caratteri di sovrapposizione.
    Ogni chunk mantiene il numero di pagina in cui inizia."""
    size = max(200, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    chunks: list[tuple[int | None, str]] = []
    for page, raw in pages:
        units = _pieces(_normalize(raw), size)
        buf: list[str] = []
        for unit in units:
            if buf and len("\n".join(buf)) + len(unit) + 1 > size:
                chunks.append((page, "\n".join(buf)))
                # mantieni la coda come contesto per il chunk successivo
                tail, length = [], 0
                for u in reversed(buf):
                    if length + len(u) > overlap:
                        break
                    tail.insert(0, u)
                    length += len(u) + 1
                # la sovrapposizione non deve far superare `size` al chunk successivo
                while tail and length + len(unit) > size:
                    length -= len(tail.pop(0)) + 1
                buf = tail
            buf.append(unit)
        if buf:
            chunks.append((page, "\n".join(buf)))
    # elimina frammenti trascurabili
    return [(p, t) for p, t in chunks if len(t.strip()) >= 20]
