"""Build the charter index (FR-04).

  python -m ingest.build_indexes --dry-run          # no Azure: chunk everything, write ingest/out/*.jsonl + preview
  python -m ingest.build_indexes                    # create/update the index + synonym map, scan, embed, upload
  python -m ingest.build_indexes --recreate
"""
from __future__ import annotations

import argparse
import json
import time

from . import config
from .chunker import chunk_document, chunk_stats
from .embedder import AzureOpenAIEmbedder, HashEmbedder
from .injection import heuristic, shield
from .loaders import load_all

DOMAIN = "charters"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="chunk only; no Azure calls, no cost")
    ap.add_argument("--recreate", action="store_true", help="delete and recreate the index first")
    ap.add_argument("--percentile", type=float, default=80.0, help="semantic breakpoint percentile (default 80)")
    args = ap.parse_args()
    if not args.dry_run and not config.azure_configured():
        ap.error("AZURE_SEARCH_ENDPOINT / AZURE_OPENAI_ENDPOINT not set (see .env.example); use --dry-run")

    embedder = HashEmbedder() if args.dry_run else AzureOpenAIEmbedder()
    docs = load_all()
    t0 = time.time()
    chunks = [c for d in docs for c in chunk_document(d, embedder, args.percentile)]
    print(f"Loaded {len(docs)} charters ({', '.join(sorted({d.file_type for d in docs}))}) -> {chunk_stats(chunks)}")
    missing = [d.source_path for d in docs if not d.initiative_id]
    if missing:
        print(f"  WARNING: no initiative id for {missing}")

    flags, method = ([heuristic(c.text) for c in chunks], "heuristic") if args.dry_run else shield([c.text for c in chunks])
    for c, f in zip(chunks, flags):
        c.injection_flag = bool(f)
    flagged = [f"{c.id} ({c.section})" for c in chunks if c.injection_flag]
    print(f"Injection scan ({method}): {len(flagged)} passage(s) flagged {flagged}")

    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        with (config.OUT_DIR / f"{DOMAIN}.jsonl").open("w", encoding="utf-8") as fh:
            for c in chunks:
                d = c.to_search_doc(); d.pop("content_vector")
                fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        with (config.OUT_DIR / f"{DOMAIN}_preview.md").open("w", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(f"### `{c.id}` {c.initiative_id} p{c.page or '-'} {len(c.text)} chars"
                         f"{' INJECTION' if c.injection_flag else ''}\n**{c.section_path}**\n\n{c.text}\n\n---\n\n")
        print(f"  wrote ingest/out/{DOMAIN}.jsonl and {DOMAIN}_preview.md")
        return

    from .search_index import ensure_index, ensure_synonym_map, index_client, search_client, upload_chunks

    iclient = index_client()
    syn = ensure_synonym_map(iclient, DOMAIN)
    name = ensure_index(iclient, DOMAIN, recreate=args.recreate)
    print(f"  index {name} ready (synonym map {syn}); embedding {len(chunks)} passages ...")
    for c, v in zip(chunks, embedder.embed([c.content for c in chunks])):
        c.content_vector = v
    ok = upload_chunks(name, chunks)
    time.sleep(2)
    print(f"  uploaded {ok}/{len(chunks)}; index reports {search_client(name).get_document_count()} docs "
          f"({time.time() - t0:.1f}s, {embedder.total_tokens} embedding tokens, "
          f"~${embedder.total_tokens / 1e6 * 0.02:.4f})")


if __name__ == "__main__":
    main()
