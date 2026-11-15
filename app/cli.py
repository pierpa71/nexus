"""Riga di comando della knowledge base: indicizzazione massiva, ricerca e importazione da Tabularium.

Con Docker/Podman:  docker compose exec nexus python -m app.cli <comando>

    python -m app.cli ingest /percorso/archivio             # cartella (ricorsiva) o singoli file
    python -m app.cli ingest /manuali --lang en             # forza la lingua (default: automatica)
    python -m app.cli search "penali per ritardata consegna"
    python -m app.cli list
    python -m app.cli delete 12
    python -m app.cli reembed                               # calcola gli embedding mancanti
    python -m app.cli import-tabularium postgresql://tabularium:tabularium@host:5432/tabularium \\
                      [--files /cartella/originali ...]     # importa documenti e passaggi da Tabularium
"""
import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

from . import kbdb, parsing, rag


async def cmd_ingest(paths, lang=None):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in parsing.SUPPORTED]
        elif p.is_file():
            files.append(p)
        else:
            print(f"! non trovato: {p}", file=sys.stderr)
    ok = 0
    for i, f in enumerate(files, 1):
        try:
            r = await rag.register_upload(f.name, f.read_bytes(), lang)
            if r["status"] == "duplicate":
                print(f"[{i}/{len(files)}] duplicato  {f}  (già presente come «{r['existing']}»)", flush=True)
                continue
            await rag.ingest(r["id"])
            d = await kbdb.get_document(r["id"])
            if d["status"] == "error":
                print(f"[{i}/{len(files)}] errore     {f}: {d['error']}", file=sys.stderr, flush=True)
                continue
            ok += 1
            langs = "+".join(d["languages"] or [])
            ocr_info = f", OCR su {d['ocr_pages']} pag." if d["ocr_pages"] else ""
            print(f"[{i}/{len(files)}] indicizzato {f}  ({d['num_chunks']} passaggi, {langs}{ocr_info})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(files)}] errore     {f}: {e}", file=sys.stderr, flush=True)
    print(f"\nIndicizzati {ok} nuovi documenti su {len(files)}.")


async def cmd_search(query, k):
    for i, h in enumerate(await rag.search(query, k), 1):
        page = f", pag. {h['page']}" if h["page"] else ""
        sim = f"sim. {h['score']:.2f}" if h["score"] is not None else "solo lessicale"
        print(f"[{i}] {h['name']}{page}  ({sim}, {'+'.join(h['matched'])})")
        print("    " + h["text"][:300].replace("\n", " ") + "\n")


async def cmd_list():
    for d in await kbdb.list_documents():
        extra = f", OCR {d['ocr_pages']} pag." if d["ocr_pages"] else ""
        flag = "" if d["enabled"] else "  [disattivato]"
        print(f"{d['id']:>5}  {d['filename']}  ({d['status']}, {d['num_chunks']} passaggi, "
              f"{'+'.join(d['languages'] or [])}{extra}){flag}")


async def cmd_reembed():
    n = await rag.reembed_pending(lambda done: print(f"  {done} passaggi…", flush=True))
    if rag._reembed["error"]:
        sys.exit(f"Interrotto: {rag._reembed['error']}")
    print(f"Embedding calcolati per {n} passaggi.")


async def cmd_import_tabularium(url, folders):
    """Copia documenti e passaggi (testo, pagina, lingua, pagine OCR) da un database Tabularium.
    Gli embedding vengono ricalcolati con il modello configurato in Nexus. Con --files, i file originali
    trovati nelle cartelle indicate (riconosciuti dall'hash SHA-256) vengono allegati ai documenti."""
    import psycopg
    from psycopg.rows import dict_row

    originals = {}
    for folder in folders or []:
        for f in Path(folder).rglob("*"):
            if f.is_file() and f.suffix.lower() in parsing.SUPPORTED:
                data = f.read_bytes()
                originals[hashlib.sha256(data).hexdigest()] = data
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as src:
        docs = await (await src.execute(
            "SELECT id, filename, sha256, size_bytes, num_pages, language, metadata FROM documents ORDER BY id")).fetchall()
        print(f"Tabularium: {len(docs)} documenti")
        for d in docs:
            rows = await (await src.execute(
                "SELECT page, content, lang::text AS lang FROM chunks WHERE document_id=%s ORDER BY chunk_index",
                (d["id"],))).fetchall()
            meta = d["metadata"] or {}
            r = await rag.import_text_document(
                d["filename"], [(r["page"], r["content"]) for r in rows], sha=d["sha256"],
                langs=[r["lang"] for r in rows], language_=d["language"], ocr_pages=meta.get("ocr_pages") or [],
                source="tabularium", size=d["size_bytes"] or 0, num_pages=d["num_pages"],
                original=originals.get(d["sha256"]), background=False)
            att = " + file originale" if d["sha256"] in originals and r["status"] == "imported" else ""
            print(f"  {r['status']:10} {d['filename']}  ({len(rows)} passaggi{att})", flush=True)
    print("Calcolo degli embedding con il modello di Nexus…")
    await cmd_reembed()


async def run(args):
    await kbdb.init_schema()
    try:
        if args.cmd == "ingest":
            await cmd_ingest(args.paths, args.lang)
        elif args.cmd == "search":
            await cmd_search(args.query, args.k)
        elif args.cmd == "list":
            await cmd_list()
        elif args.cmd == "delete":
            ok = await kbdb.delete_document(args.id)
            rag.delete_original(args.id)
            print("Eliminato." if ok else "Documento non trovato.")
        elif args.cmd == "reembed":
            await cmd_reembed()
        elif args.cmd == "import-tabularium":
            await cmd_import_tabularium(args.url, args.files)
    finally:
        await kbdb.close()


def main():
    ap = argparse.ArgumentParser(prog="nexus-kb", description="Knowledge base di Nexus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="indicizza file o cartelle")
    p.add_argument("paths", nargs="+")
    p.add_argument("--lang", default=None, help="auto (default), it, en, fr, de, es")
    p = sub.add_parser("search", help="ricerca ibrida, senza LLM")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=6)
    sub.add_parser("list", help="elenco documenti")
    p = sub.add_parser("delete", help="elimina un documento")
    p.add_argument("id", type=int)
    sub.add_parser("reembed", help="calcola gli embedding mancanti")
    p = sub.add_parser("import-tabularium", help="importa l'archivio di Tabularium")
    p.add_argument("url", help="DATABASE_URL di Tabularium")
    p.add_argument("--files", nargs="*", default=[], help="cartelle con i file originali da allegare")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
