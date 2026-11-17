"""Response objects returned by the API (rendered by the UI, spoken by the voice layer)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ResponseType = Literal["answer", "not_found", "refusal", "clarify", "greeting", "error"]

FALLBACK = "I do not have that in the data provided."
PII_REFUSAL = ("I can't share that. The approved portfolio data only includes each initiative's executive sponsor and "
               "product owner (name and job title); other personal details are not available to me.")


class Citation(BaseModel):
    label: str                      # the marker used in the answer text: S1 (scorecard) or C2 (charter)
    kind: Literal["sql", "charter"]
    evidence_id: str
    title: str
    section: str = ""
    initiative_id: str = ""
    page: int | None = None
    source_file: str = ""
    excerpt: str = ""
    query: str = ""                 # SQL evidence: the executed query
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    injection_flag: bool = False


class GuardViolation(BaseModel):
    kind: Literal["unverified_number", "status_mismatch", "invalid_citation", "uncited_figure", "fallback_wording"]
    detail: str
    text: str = ""


class GuardReport(BaseModel):
    passed: bool = True
    numbers_checked: int = 0
    numbers_verified: int = 0
    charter_figures: int = 0
    violations: list[GuardViolation] = Field(default_factory=list)
    removed_sentences: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)       # e.g. ignored instructions found in charters


class Verdict(BaseModel):
    groundedness: int
    relevance: int
    completeness: int
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_aspects: list[str] = Field(default_factory=list)
    action: Literal["pass", "revise"]
    critique: str = ""
    auto_checks: list[str] = Field(default_factory=list)
    skipped: bool = False                                 # LLM judge skipped (numeric answer fully verified in code)


class Confidence(BaseModel):
    label: Literal["high", "medium", "low", "none"]
    retrieval: str
    groundedness: int | None = None
    relevance: int | None = None
    completeness: int | None = None
    reason: str = ""


class ClosestMatch(BaseModel):
    source_file: str
    section: str
    excerpt: str


class AnswerResponse(BaseModel):
    interaction_id: str
    type: ResponseType
    question: str
    standalone_question: str = ""
    route: str = ""
    answer_markdown: str = ""
    format: str = "prose"
    spoken_summary: str = ""
    citations: list[Citation] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    confidence: Confidence | None = None
    closest_matches: list[ClosestMatch] = Field(default_factory=list)
    clarification_options: list[str] = Field(default_factory=list)
    guardrails: GuardReport = Field(default_factory=GuardReport)
    trace: dict[str, Any] = Field(default_factory=dict)
