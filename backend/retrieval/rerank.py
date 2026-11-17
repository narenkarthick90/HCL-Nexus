"""LLM rerank fallback, used when the semantic ranker is unavailable (other region, quota exhausted)
or when explicitly requested for the ablation study. Scores each candidate 0-3 against the question."""
from __future__ import annotations

from ..llm import json_completion, strict_schema
from .models import Evidence

SYSTEM = """You grade how well each passage answers a question.
Scores: 3 = directly answers it; 2 = relevant and partly answers it; 1 = same topic but does not answer it; \
0 = unrelated. Grade every passage id that is given. Judge only by the passage text."""

SCHEMA = strict_schema({
    "scores": {"type": "array", "items": strict_schema({"id": {"type": "string"}, "score": {"type": "integer"}})},
})


async def llm_rerank(query: str, evidence: list[Evidence], max_candidates: int = 12) -> list[Evidence]:
    cands = evidence[:max_candidates]
    if not cands:
        return evidence
    listing = "\n\n".join(f"[{e.id}] {e.source_file} > {e.section_path}\n{e.text[:700]}" for e in cands)
    data = await json_completion(SYSTEM, f"Question: {query}\n\nPassages:\n\n{listing}", "rerank", SCHEMA, role="rerank",
                                 max_tokens=800)
    scores = {s["id"]: max(0, min(3, int(s["score"]))) for s in data.get("scores", [])}
    for e in cands:
        e.llm_score = float(scores.get(e.id, 0))
    ranked = sorted(cands, key=lambda e: (e.llm_score or 0, e.rrf_score), reverse=True)
    return ranked + evidence[max_candidates:]
