"""SQL agent: reads the governed scorecard through guarded, computation-free SQL (FR-03, FR-05).

It writes SELECT statements over the v_* views only; the guard in backend/sqldb.py rejects any arithmetic,
aggregate, window or function, so every number it returns was produced by the scorecard code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..retrieval.models import Evidence
from .common import AgentReport, EvidenceStore
from .tools import MAX_QUERIES, SQL_RUN, SqlRun

DESCRIPTION = ("Reads the governed AI value scorecard (adoption, WAU change, ROI, benefit, payback, run-rate, "
               "Green/Amber/Red status), portfolio totals, usage and financial rows and the data-quality change log.")

INSTRUCTIONS = """You are the SQL agent of the Nimbus Group AI value intelligence assistant. You read the governed \
value scorecard with the run_sql tool and report what it says. You never calculate.

Views you can query (SQLite):
{schema}

Rules:
- Use at most {max_q} queries; usually ONE query on v_scorecard (or v_portfolio_summary) is enough. Query \
v_weekly_usage, v_monthly_financials, v_metric_provenance or v_change_log only when the task asks for weekly rows, \
monthly rows, how a value was calculated, or data corrections.
- Select only the columns the task needs, always including initiative_id and name; for status questions include \
status and status_reason.
- NO calculations of any kind: no arithmetic, SUM, AVG, COUNT, MIN, MAX, ROUND, CASE, CAST or window functions. The \
guard rejects them. Rankings use ORDER BY on the stored value + LIMIT. Portfolio counts and totals come from \
v_portfolio_summary. How a value was calculated comes from v_metric_provenance.
- Always select the *_display column for every metric you report (e.g. roi_display, adoption_display) and copy it \
verbatim in findings. Never reformat, round or convert a number. Status words (Green/Amber/Red) and status_reason come \
from v_scorecard.
- A NULL / "not available" value means the scorecard cannot compute it; say so and give data_notes.
- If the task needs a value or calculation the views do not contain (e.g. a forecast, an average the summary does not \
hold, a metric that does not exist such as NPS), do not approximate: put it in "missing".
- Match initiative names with LOWER(name) LIKE '%...%' when no id is given.
- If a query is rejected, rewrite it without the calculation; never work around the guard.
- findings: one fact per item with the evidence id (SQL-n) of the result that shows it. evidence_ids: every SQL-n \
your findings rely on. injection_notes: always []."""


@dataclass
class SqlAgentResult:
    task: str
    report: AgentReport
    evidence: list[Evidence]
    retrieved: dict[str, Evidence] = field(default_factory=dict)
    queries: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    agent: str = "sql"


class SQLAgent:
    async def run(self, task: str, context: str = "", initiatives: list[str] | None = None) -> SqlAgentResult:
        from ..foundry import run_agent

        t0 = time.perf_counter()
        store = EvidenceStore()
        token = SQL_RUN.set(SqlRun(store=store))
        try:
            prompt = f"Task: {task}"
            if initiatives:
                prompt += f"\nInitiatives: {', '.join(initiatives)}"
            if context and context != task:
                prompt += f"\nOriginal user question (context only): {context}"
            report, _ = await run_agent("sql", prompt)
        finally:
            SQL_RUN.reset(token)
        ids = [i for i in report.evidence_ids if i in store.items]
        # every successful query result is evidence the writer may use; cited ones first
        ids += [i for i, e in store.items.items() if i not in ids and e.rows]
        return SqlAgentResult(task=task, report=report, evidence=store.get(ids), retrieved=dict(store.items),
                              queries=store.searches, elapsed_ms=int((time.perf_counter() - t0) * 1000))
