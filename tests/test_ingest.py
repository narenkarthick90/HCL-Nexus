"""Charter ingestion (FR-04): four formats, metadata, structure-aware chunks, injection flagging. No Azure calls."""
from __future__ import annotations

import pytest

from ingest.chunker import chunk_document
from ingest.embedder import HashEmbedder
from ingest.injection import heuristic
from ingest.loaders import load_all


@pytest.fixture(scope="module")
def docs():
    return load_all()


@pytest.fixture(scope="module")
def chunks(docs):
    emb = HashEmbedder()
    return [c for d in docs for c in chunk_document(d, emb)]


def test_ten_charters_in_four_formats_with_initiative_ids(docs):
    assert len(docs) == 10
    assert {d.file_type for d in docs} == {"pdf", "docx", "md", "html"}
    assert sorted(d.initiative_id for d in docs) == [f"INIT-{i:02d}" for i in range(1, 11)]
    assert all(d.title.endswith("Initiative Charter") for d in docs)


def test_every_charter_has_the_same_sections_regardless_of_format(docs):
    core = {"Initiative overview", "Benefit hypothesis", "KPIs and measurement", "Governance", "Status notes"}
    for d in docs:
        tops = {s.path[0] for s in d.sections}
        assert core <= tops, (d.source_path, tops)
    no_risks = [d.initiative_id for d in docs if "Risks and mitigations" not in {s.path[0] for s in d.sections}]
    assert no_risks == ["INIT-10"]


def test_chunk_metadata_for_citations(chunks):
    for c in chunks:
        assert c.initiative_id.startswith("INIT-") and c.source_file and c.section
        assert c.source_file in c.source_url
        assert c.id.startswith(c.initiative_id)
    assert len({c.id for c in chunks}) == len(chunks)


def test_tables_and_lists_never_split(chunks):
    risks = [c for c in chunks if c.section == "Risks and mitigations"]
    assert risks and all(c.text.startswith("| Risk | Mitigation |") for c in risks)
    for c in chunks:
        if "|---|" in c.text:
            assert c.text.count("|---|") >= 1 and not c.text.rstrip().endswith("|---|")


def test_boilerplate_removed(chunks):
    assert not any("Sign out" in c.text or "Internal use only" in c.text for c in chunks)


def test_injection_heuristic_flags_only_the_planted_note(chunks):
    flagged = [c.id for c in chunks if heuristic(c.text)]
    assert flagged == ["INIT-07-s08-c00"]
