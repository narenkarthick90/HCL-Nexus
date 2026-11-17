"""Query expansion (FR-03): multi-query rewrites + step-back question + HyDE passage, in one cheap call.

- paraphrases / keywords widen the BM25 leg (alongside the index synonym maps)
- paraphrases / step-back / HyDE each become an extra vector query
- the original question is always kept and drives the semantic reranker
"""
from __future__ import annotations

from ..llm import json_completion, strict_schema
from .models import QueryExpansion

PROMPT_VERSION = "expand-charters-v1"

SYSTEM = """You rewrite questions so they can be searched in a collection of AI initiative charters. Each charter has these sections: Initiative overview (objective), Scope, Benefit hypothesis, KPIs and measurement, Risks and mitigations, Governance (sponsor and product owner), Dependencies, Status notes.
Return JSON with:
- paraphrases: exactly 2 rewordings that use the formal vocabulary a charter would use. Make them differ from each other and from the original. Keep every initiative name and id from the question.
- keywords: 4 to 10 search terms and synonyms separated by spaces, no punctuation or operators.
- step_back: one more general question about the underlying topic.
- hyde: 2 to 3 sentences written as if quoted from the charter section that answers the question. Invented details are acceptable because this text is only used for vector search and is never shown to anyone.
Do not answer the question yourself outside the hyde field."""

SCHEMA = strict_schema({
    "paraphrases": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "string"},
    "step_back": {"type": "string"},
    "hyde": {"type": "string"},
})


async def expand_query(query: str) -> QueryExpansion:
    data = await json_completion(
        SYSTEM + f"\n[{PROMPT_VERSION}]",
        query, "query_expansion", SCHEMA, role="expansion", max_tokens=600)
    data["paraphrases"] = [p for p in data.get("paraphrases", []) if p.strip()][:2]
    return QueryExpansion(**data)
