"""Evidence objects passed between the tools, agents, writer and judge.

Two kinds of evidence:
  charter - a retrieved charter passage (citation: file name > section)
  sql     - the exact result of a governed scorecard query (citation: the query and its rows)
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low", "none"]

# Thresholds on normalised relevance (semantic reranker score / 4, or LLM score / 3).
CONFIDENCE_THRESHOLDS: dict[str, float] = {"high": 0.55, "medium": 0.45, "low": 0.30}
MIN_RELEVANCE = CONFIDENCE_THRESHOLDS["low"]


def confidence_for(relevance: float | None) -> Confidence:
    if relevance is None:
        return "none"
    for label in ("high", "medium", "low"):
        if relevance >= CONFIDENCE_THRESHOLDS[label]:
            return label  # type: ignore[return-value]
    return "none"


class Evidence(BaseModel):
    id: str                                   # charter chunk id (INIT-03-s05-c00) or SQL-n
    kind: Literal["charter", "sql"] = "charter"
    initiative_id: str = ""
    title: str = ""
    section: str = ""
    section_path: str = ""
    page: int | None = None
    source_file: str = ""
    source_url: str = ""
    file_type: str = ""
    text: str                                 # passage text, or the SQL result as a markdown table
    parent_text: str = ""
    caption: str = ""
    injection_flag: bool = False              # the passage contains instructions aimed at an AI system
    rrf_score: float = 0.0
    rerank_score: float | None = None         # semantic ranker, 0-4
    llm_score: float | None = None            # LLM rerank fallback, 0-3
    found_by: list[str] = Field(default_factory=list)
    # SQL evidence
    sql: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)

    @property
    def exact(self) -> bool:
        return self.kind == "sql"

    @property
    def relevance(self) -> float | None:
        if self.exact:
            return 1.0
        if self.rerank_score is not None:
            return self.rerank_score / 4.0
        if self.llm_score is not None:
            return self.llm_score / 3.0
        return None

    @property
    def confidence(self) -> Confidence:
        return confidence_for(self.relevance)

    @property
    def citation(self) -> str:
        if self.exact:
            return f"Governed scorecard ({', '.join(sorted(set(_views(self.sql)))) or 'scorecard'})"
        page = f", p. {self.page}" if self.page else ""
        return f"{self.source_file} > {self.section_path}{page}"

    @property
    def excerpt(self) -> str:
        if self.exact:
            return self.sql
        return self.caption or (self.text if len(self.text) <= 300 else self.text[:297].rsplit(" ", 1)[0] + " ...")


def _views(sql: str) -> list[str]:
    import re

    return re.findall(r"\b(v_[a-z_]+)\b", sql or "")


class QueryExpansion(BaseModel):
    paraphrases: list[str] = Field(default_factory=list)
    keywords: str = ""
    step_back: str = ""
    hyde: str = ""


class RetrievalResult(BaseModel):
    query: str
    initiatives: list[str] = Field(default_factory=list)
    evidence: list[Evidence]                  # ranked, best first
    expansion: QueryExpansion | None = None
    semantic_used: bool = False
    llm_rerank_used: bool = False
    timings_ms: dict[str, int] = Field(default_factory=dict)

    @property
    def top_relevance(self) -> float | None:
        scores = [e.relevance for e in self.evidence if e.relevance is not None]
        return max(scores) if scores else None

    @property
    def confidence(self) -> Confidence:
        return confidence_for(self.top_relevance)

    @property
    def relevant(self) -> list[Evidence]:
        return [e for e in self.evidence if e.relevance is None or e.relevance >= MIN_RELEVANCE]
