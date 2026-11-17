"""Add (or replace) one charter in the charter index; used by the API's upload endpoint."""
from __future__ import annotations

import re
from pathlib import Path

from .chunker import chunk_document, chunk_stats
from .config import INDEXES
from .embedder import AzureOpenAIEmbedder
from .injection import shield
from .loaders import LOADERS, load_document
from .search_index import search_client

MAX_BYTES = 10 * 1024 * 1024
_ID = re.compile(r"^INIT-\d{2}$")


def ingest_file(path: Path, initiative_id: str) -> dict:
    """Synchronous (call via a worker thread). Returns charter metadata, chunk statistics and injection flags."""
    if not _ID.match(initiative_id):
        raise ValueError("initiative_id must look like INIT-07")
    if path.suffix.lower() not in LOADERS:
        raise ValueError(f"unsupported format {path.suffix}; supported: {sorted(LOADERS)}")
    doc = load_document(path, "charters")
    if not doc.sections:
        raise ValueError("no text could be extracted from the document")
    doc.initiative_id, doc.doc_id = initiative_id, f"{initiative_id}-UPLOAD-{re.sub(r'[^A-Za-z0-9]', '', path.stem)[:24]}"
    embedder = AzureOpenAIEmbedder()
    chunks = chunk_document(doc, embedder)
    flags, method = shield([c.text for c in chunks])
    for c, f, v in zip(chunks, flags, embedder.embed([c.content for c in chunks])):
        c.injection_flag, c.content_vector = bool(f), v

    client = search_client(INDEXES["charters"])
    old = [r["id"] for r in client.search(search_text="*", filter=f"doc_id eq '{doc.doc_id}'", select=["id"], top=1000)]
    if old:
        client.delete_documents([{"id": i} for i in old])
    results = client.upload_documents([c.to_search_doc() for c in chunks])
    failed = [r.key for r in results if not r.succeeded]
    if failed:
        raise RuntimeError(f"{len(failed)} chunks failed to upload")
    return {"doc_id": doc.doc_id, "initiative_id": initiative_id, "title": doc.title, "file_type": doc.file_type,
            "sections": len(doc.sections), "replaced_chunks": len(old), "embedding_tokens": embedder.total_tokens,
            "injection_scan": method, "flagged_passages": [c.id for c in chunks if c.injection_flag],
            **chunk_stats(chunks)}
