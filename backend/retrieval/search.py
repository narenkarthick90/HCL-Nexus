"""Hybrid retrieval over the charter index (FR-04 / FR-06).

One Azure AI Search request per (sub-)query, which keeps us inside the free tier's semantic-ranker quota:
  - BM25 leg: original question + expansion keywords (the index synonym map expands further)
  - vector legs: original question, 2 paraphrases, step-back question, HyDE passage (weighted)
  - server-side Reciprocal Rank Fusion of all legs, optionally filtered to given initiatives
  - semantic ranker re-scores the fused results against the ORIGINAL question and returns an extractive caption
If the semantic ranker is unavailable, an LLM rerank produces the relevance scores instead.

CLI:  python -m backend.retrieval.search "what are the risks of the claims bot?" [--initiative INIT-06]
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorizedQuery

from ingest.search_index import SEMANTIC_CONFIG

from .. import settings
from ..llm import embed
from .expand import expand_query
from .models import Evidence, QueryExpansion, RetrievalResult
from .rerank import llm_rerank

INDEX = settings.INDEXES["charters"]
SELECT = ["id", "doc_id", "parent_id", "title", "section_path", "page", "source_url", "file_type", "text",
          "parent_text", "initiative_id", "section", "source_file", "injection_flag"]
VECTOR_WEIGHTS = {"question": 1.0, "hyde": 1.0, "paraphrase": 0.7, "step_back": 0.5}
_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_ID = re.compile(r"^INIT-\d{2}$")

_client: SearchClient | None = None
_semantic_available = True


def client() -> SearchClient:
    global _client
    if _client is None:
        _client = SearchClient(settings.AZURE_SEARCH_ENDPOINT, INDEX, AzureKeyCredential(settings.AZURE_SEARCH_API_KEY))
    return _client


async def close_clients() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def _caption(r: dict[str, Any]) -> str:
    caps = r.get("@search.captions") or []
    if not caps:
        return ""
    c = caps[0]
    return (c.get("text") if isinstance(c, dict) else getattr(c, "text", "")) or ""


def to_evidence(r: dict[str, Any], query: str) -> Evidence:
    fields = {k: r.get(k) for k in SELECT if r.get(k) is not None and k not in ("doc_id", "parent_id")}
    return Evidence(**fields, caption=_caption(r), rrf_score=float(r.get("@search.score") or 0.0),
                    rerank_score=r.get("@search.reranker_score"), found_by=[query])


def initiative_filter(ids: list[str] | None) -> str | None:
    ids = [i for i in (ids or []) if _ID.match(i)]
    return f"search.in(initiative_id, '{','.join(ids)}', ',')" if ids else None


async def _search(query: str, bm25_text: str, vectors: list[tuple[str, list[float]]], top: int, k: int,
                  semantic: bool, flt: str | None) -> list[Evidence]:
    vqs = [VectorizedQuery(vector=v, k_nearest_neighbors=k, fields="content_vector", weight=VECTOR_WEIGHTS[kind])
           for kind, v in vectors]
    kwargs: dict[str, Any] = dict(search_text=bm25_text, vector_queries=vqs, top=top, select=SELECT,
                                  search_mode="any", filter=flt)
    if semantic:
        kwargs.update(query_type="semantic", semantic_configuration_name=SEMANTIC_CONFIG, semantic_query=query,
                      query_caption="extractive|highlight-false")
    results = await client().search(**kwargs)
    return [to_evidence(r, query) async for r in results]


async def section_lookup(initiative_id: str, section: str) -> list[Evidence]:
    """All passages of one charter section (e.g. 'Risks and mitigations'), no ranking. Used by the weekly report."""
    flt = f"initiative_id eq '{initiative_id}' and section eq '{section}'"
    results = await client().search(search_text="*", filter=flt, select=SELECT, top=20)
    return [to_evidence(r, f"{initiative_id} {section}") async for r in results]


async def retrieve(query: str, initiatives: list[str] | None = None, *, top: int = 8, expand: bool = True,
                   hyde: bool = True, semantic: bool = True, llm_rerank_fallback: bool = True,
                   force_llm_rerank: bool = False, k: int = 30) -> RetrievalResult:
    """Ranked charter passages for `query`, optionally restricted to the given initiative ids."""
    global _semantic_available
    t0 = time.perf_counter()
    timings: dict[str, int] = {}
    flt = initiative_filter(initiatives)

    expansion: QueryExpansion | None = await expand_query(query) if (expand or hyde) else None
    timings["expand"] = int((time.perf_counter() - t0) * 1000)

    texts: list[tuple[str, str]] = [("question", query)]
    if expansion and expand:
        texts += [("paraphrase", p) for p in expansion.paraphrases]
        if expansion.step_back:
            texts.append(("step_back", expansion.step_back))
    if expansion and hyde and expansion.hyde:
        texts.append(("hyde", expansion.hyde))
    t1 = time.perf_counter()
    vectors = list(zip([kind for kind, _ in texts], await embed([t for _, t in texts])))
    timings["embed"] = int((time.perf_counter() - t1) * 1000)

    bm25 = _SPECIAL.sub(" ", query if not (expansion and expand) else f"{query} {expansion.keywords}")
    use_semantic = semantic and _semantic_available and not force_llm_rerank
    t2 = time.perf_counter()
    try:
        evidence = await _search(query, bm25, vectors, top, k, use_semantic, flt)
    except HttpResponseError as e:
        if not use_semantic:
            raise
        _semantic_available = False
        print(f"[retrieval] semantic ranker unavailable ({str(e).splitlines()[0][:160]}); using LLM rerank")
        use_semantic = False
        evidence = await _search(query, bm25, vectors, top, k, False, flt)
    timings["search"] = int((time.perf_counter() - t2) * 1000)

    llm_used = False
    if not use_semantic and (force_llm_rerank or llm_rerank_fallback):
        t3 = time.perf_counter()
        evidence = await llm_rerank(query, evidence)
        timings["llm_rerank"] = int((time.perf_counter() - t3) * 1000)
        llm_used = True
    timings["total"] = int((time.perf_counter() - t0) * 1000)
    return RetrievalResult(query=query, initiatives=initiatives or [], evidence=evidence, expansion=expansion,
                           semantic_used=use_semantic, llm_rerank_used=llm_used, timings_ms=timings)


async def _cli() -> None:
    import argparse

    from ..llm import usage_scope

    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--initiative", action="append")
    ap.add_argument("--no-expand", action="store_true")
    ap.add_argument("--no-semantic", action="store_true")
    a = ap.parse_args()
    with usage_scope() as usage:
        res = await retrieve(a.query, a.initiative, expand=not a.no_expand, hyde=not a.no_expand,
                             semantic=not a.no_semantic)
    await close_clients()
    print(f"confidence={res.confidence} semantic={res.semantic_used} timings={res.timings_ms}")
    for i, e in enumerate(res.evidence, 1):
        rel = f"{e.relevance:.2f}" if e.relevance is not None else "  - "
        flag = " [INJECTION]" if e.injection_flag else ""
        print(f"{i:>2}. rel={rel} {e.id:<18} {e.citation}{flag}\n      {e.excerpt[:150]}")
    print("usage:", usage.summary())


if __name__ == "__main__":
    asyncio.run(_cli())
