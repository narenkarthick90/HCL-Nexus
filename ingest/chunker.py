"""Structure-aware + semantic chunking (FR-02). No fixed token sizes.

1. Structural: every heading section is a *parent*. Tables and lists are atomic blocks.
2. Semantic: inside a section, sentences are embedded with a one-sentence context window on each
   side; the cosine distance between neighbouring windows is computed, and a break is placed where
   the distance exceeds the section's `percentile`-th percentile (a topic shift). Breaks are never
   placed directly before a table or list (so "the following:" stays with its list) and never
   produce fragments smaller than `min_units`.
3. Each child chunk gets a contextual header (title > section path, document summary, version)
   prepended for embedding and BM25, and carries its parent's full text for the answer writer.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

import numpy as np

from .embedder import Embedder
from .models import Chunk, Document, Section

SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
MIN_UNITS_TO_SPLIT = 6      # sections with fewer sentence-units are kept whole
MIN_UNITS = 3               # smallest allowed fragment (an atomic block counts as MIN_UNITS)
MIN_DISTANCE = 0.05         # ignore "topic shifts" between near-identical windows
INTRA_PARAGRAPH_DISCOUNT = 0.8   # a shift inside a paragraph must be clearer than one at a paragraph boundary
SAFETY_MAX_CHARS = 6000     # embedding-model safety cap only; never reached by normal sections


@dataclass
class Unit:
    text: str
    atomic: bool
    para: int
    page: int | None

    @property
    def weight(self) -> int:
        return MIN_UNITS if self.atomic else 1


def section_units(section: Section) -> list[Unit]:
    units: list[Unit] = []
    for pi, b in enumerate(section.blocks):
        if b.kind == "p":
            units += [Unit(s.strip(), False, pi, b.page) for s in SENT_RE.split(b.text) if s.strip()]
        else:
            units.append(Unit(b.text, True, pi, b.page))
    return units


def semantic_breaks(units: list[Unit], embedder: Embedder, percentile: float, window: int = 1) -> list[int]:
    """Return indices i such that a chunk boundary falls between units[i] and units[i+1]."""
    if len(units) < MIN_UNITS_TO_SPLIT:
        return []
    ctx = [" ".join(u.text for u in units[max(0, i - window): i + window + 1]) for i in range(len(units))]
    v = np.asarray(embedder.embed(ctx), dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-9
    dist = 1.0 - np.sum(v[:-1] * v[1:], axis=1)
    for i in range(len(dist)):
        if units[i + 1].atomic:                         # never separate an intro sentence from its list/table
            dist[i] = -1.0
        elif units[i].para == units[i + 1].para:        # prefer breaking at paragraph boundaries
            dist[i] *= INTRA_PARAGRAPH_DISCOUNT
    valid = dist[dist >= 0]
    if valid.size == 0:
        return []
    thr = max(float(np.percentile(valid, percentile)), MIN_DISTANCE)
    candidates = [i for i, d in enumerate(dist) if d > thr]

    breaks, start = [], 0
    for i in candidates:
        if sum(u.weight for u in units[start:i + 1]) >= MIN_UNITS:
            breaks.append(i)
            start = i + 1
    if breaks and sum(u.weight for u in units[breaks[-1] + 1:]) < MIN_UNITS:
        breaks.pop()                                    # trailing fragment too small: merge back
    return breaks


def _join(units: list[Unit]) -> str:
    out, cur_para, cur = [], None, []
    for u in units:
        if u.para != cur_para and cur:
            out.append(" ".join(cur)); cur = []
        cur_para = u.para
        cur.append(u.text)
    if cur:
        out.append(" ".join(cur))
    return "\n\n".join(out)


def _pieces(units: list[Unit], breaks: list[int]) -> list[list[Unit]]:
    pieces, start = [], 0
    for b in breaks:
        pieces.append(units[start:b + 1]); start = b + 1
    pieces.append(units[start:])
    out: list[list[Unit]] = []
    for p in pieces:                                   # safety cap only (embedding input limit)
        while len(_join(p)) > SAFETY_MAX_CHARS and len(p) > 1:
            out.append(p[: len(p) // 2]); p = p[len(p) // 2:]
        out.append(p)
    return [p for p in out if p]


def context_header(doc: Document, section: Section) -> str:
    header = f"{doc.title} > {' > '.join(section.path)}"
    if doc.initiative_id:
        header += f"\nInitiative: {doc.initiative_id}"
    if doc.summary:
        header += f"\nAbout this document: {doc.summary}"
    if doc.version or doc.effective_date:
        header += f"\nVersion {doc.version}, effective {doc.effective_date}."
    return header


def chunk_document(doc: Document, embedder: Embedder, percentile: float = 80.0) -> list[Chunk]:
    chunks: list[Chunk] = []
    for si, section in enumerate(doc.sections):
        units = section_units(section)
        if not units:
            continue
        header = context_header(doc, section)
        parent_id = f"{doc.doc_id}-s{si:02d}"
        parent_text = f"{header}\n\n{section.text}"
        for ci, piece in enumerate(_pieces(units, semantic_breaks(units, embedder, percentile))):
            text = _join(piece)
            chunks.append(Chunk(
                id=f"{parent_id}-c{ci:02d}", doc_id=doc.doc_id, parent_id=parent_id, chunk_index=len(chunks),
                title=doc.title, section_path=" > ".join(section.path), page=piece[0].page or section.page,
                domain=doc.domain, doc_version=doc.version, effective_date=doc.effective_date, owner=doc.owner,
                source_url=doc.source_path, file_type=doc.file_type, text=text,
                content=f"{header}\n\n{text}", parent_text=parent_text, initiative_id=doc.initiative_id,
                section=section.path[0], source_file=doc.source_path.rsplit("/", 1)[-1]))
    return chunks


def chunk_stats(chunks: list[Chunk]) -> dict:
    sizes = [len(c.text) for c in chunks]
    parents = {c.parent_id for c in chunks}
    split_parents = {p for p in parents if sum(c.parent_id == p for c in chunks) > 1}
    return {
        "chunks": len(chunks), "sections": len(parents), "sections_semantically_split": len(split_parents),
        "chars_min": min(sizes), "chars_median": int(statistics.median(sizes)), "chars_max": max(sizes),
        "with_table": sum("|---|" in c.text for c in chunks),
        "with_list": sum(bool(re.search(r"(^|\n)(- |\d+\. )", c.text)) for c in chunks),
    }
