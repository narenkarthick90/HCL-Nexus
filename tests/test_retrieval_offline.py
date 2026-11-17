"""Offline retrieval helpers: confidence, citations, initiative filter, BM25 sanitiser."""
from __future__ import annotations

from backend.retrieval.models import Evidence, confidence_for
from backend.retrieval.search import _SPECIAL, initiative_filter


def test_confidence_thresholds():
    assert confidence_for(0.6) == "high" and confidence_for(0.5) == "medium"
    assert confidence_for(0.31) == "low" and confidence_for(0.1) == "none" and confidence_for(None) == "none"


def test_charter_and_sql_citations():
    c = Evidence(id="INIT-06-s05-c00", text="| Risk | Mitigation |", source_file="INIT-06_claims_triage_bot_charter.docx",
                 section_path="Risks and mitigations", rerank_score=3.0, initiative_id="INIT-06")
    assert c.citation == "INIT-06_claims_triage_bot_charter.docx > Risks and mitigations"
    assert c.relevance == 0.75 and not c.exact
    s = Evidence(id="SQL-1", kind="sql", text="t", sql="SELECT * FROM v_scorecard JOIN v_initiatives USING (initiative_id)")
    assert s.exact and s.relevance == 1.0 and s.citation == "Governed scorecard (v_initiatives, v_scorecard)"


def test_initiative_filter_only_accepts_valid_ids():
    assert initiative_filter(["INIT-01", "INIT-06"]) == "search.in(initiative_id, 'INIT-01,INIT-06', ',')"
    assert initiative_filter(["INIT-01' or 1 eq 1"]) is None
    assert initiative_filter([]) is None


def test_bm25_sanitiser_removes_query_operators():
    assert _SPECIAL.sub(" ", 'risks "claims" (bot) -foo') .split() == ["risks", "claims", "bot", "foo"]
