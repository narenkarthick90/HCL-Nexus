"""Offline tests for the Foundry agent registry, tool context isolation and the SQL tool (no Azure calls)."""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")

from backend import foundry, settings  # noqa: E402
from backend.agents import tools  # noqa: E402
from backend.agents.common import EvidenceStore  # noqa: E402

EXPECTED = {"router", "sql", "charter", "writer", "judge"}


def test_registry_has_the_five_agents_with_tools_and_outputs():
    s = foundry.specs()
    assert set(s) == EXPECTED
    assert [t.name for t in s["charter"].tools] == ["search_charters"]
    assert [t.name for t in s["sql"].tools] == ["run_sql"]
    assert not s["writer"].tools and not s["judge"].tools and not s["router"].tools
    assert all(sp.name.startswith(settings.AGENT_NAME_PREFIX) for sp in s.values())
    assert "VIEW v_scorecard" in s["sql"].instructions and "{schema}" not in s["sql"].instructions
    assert "INIT-06: Claims Triage Bot" in s["router"].instructions       # roster baked into the router


def test_prompt_agent_definitions_carry_model_tools_schema_and_effort():
    for key in ("charter", "sql", "judge"):
        spec = foundry.specs()[key]
        d = foundry.definition(spec).as_dict()
        assert d["model"] == settings.role_model(spec.role)[0]
        assert d["text"]["format"]["type"] == "json_schema" and d["text"]["format"]["strict"] is True
        if key != "judge":
            assert d["tools"][0]["parameters"]["additionalProperties"] is False


def test_definition_hash_is_stable_and_detects_changes():
    spec = foundry.specs()["judge"]
    h1 = foundry.definition_hash(foundry.definition(spec))
    assert h1 == foundry.definition_hash(foundry.definition(spec))
    changed = foundry.AgentSpec(**{**spec.__dict__, "instructions": spec.instructions + " Be brief."})
    assert foundry.definition_hash(foundry.definition(changed)) != h1


def test_tool_state_is_isolated_between_concurrent_runs():
    async def one_run(ids: list[str]) -> list[str]:
        token = tools.SEARCH_RUN.set(tools.SearchRun(store=EvidenceStore(), initiatives=ids))
        try:
            await asyncio.sleep(0.01)
            return tools.SEARCH_RUN.get().initiatives
        finally:
            tools.SEARCH_RUN.reset(token)

    async def main():
        return await asyncio.gather(one_run(["INIT-01"]), one_run(["INIT-02"]), one_run([]))

    assert asyncio.run(main()) == [["INIT-01"], ["INIT-02"], []]
    assert tools.SEARCH_RUN.get() is None


def test_tools_refuse_outside_a_run():
    assert "unavailable" in asyncio.run(tools._search_charters("x", []))
    assert "unavailable" in asyncio.run(tools._run_sql("SELECT * FROM v_scorecard"))


def test_run_sql_records_evidence_rejects_calculation_and_enforces_limit():
    async def main():
        store = EvidenceStore()
        token = tools.SQL_RUN.set(tools.SqlRun(store=store))
        try:
            outs = [await tools._run_sql("SELECT initiative_id, roi_display FROM v_scorecard WHERE status = 'Red'"),
                    await tools._run_sql("SELECT AVG(roi) FROM v_scorecard")]
            outs += [await tools._run_sql("SELECT * FROM v_portfolio_summary") for _ in range(tools.MAX_QUERIES)]
        finally:
            tools.SQL_RUN.reset(token)
        return store, outs

    store, outs = asyncio.run(main())
    e = store.items["SQL-1"]
    assert e.kind == "sql" and e.columns == ["initiative_id", "roi_display"] and len(e.rows) == 2
    assert "REJECTED" in outs[1] and "SQL-2" not in store.items
    assert "limit reached" in outs[-1]
