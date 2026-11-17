"""Function tools for the registered Foundry agents (and the local fallback).

A Foundry prompt agent owns its tool *declarations*; the code only supplies implementations, bound once per
process. Per-request state (which evidence store to fill, how many queries ran) lives in ContextVars that each
agent run sets first; asyncio tasks inherit the context, so concurrent runs never see each other's state.
Schemas are explicit and strict (additionalProperties: false), as Foundry Agent Service requires.
"""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Any

from agent_framework import tool

from .. import sqldb
from ..retrieval import retrieve
from ..retrieval.models import Evidence
from .common import EvidenceStore, format_passages

MAX_EXTRA_SEARCHES = 2
MAX_QUERIES = 4


# ---------------------------------------------------------------- charter search
@dataclass
class SearchRun:
    store: EvidenceStore
    initiatives: list[str]


SEARCH_RUN: contextvars.ContextVar[SearchRun | None] = contextvars.ContextVar("search_run", default=None)


async def _search_charters(query: str, initiative_ids: list[str]) -> str:
    run = SEARCH_RUN.get()
    if run is None:
        return "Search is unavailable outside a charter agent run."
    if len(run.store.searches) > MAX_EXTRA_SEARCHES:
        return "Search limit reached. Write the report from the passages you already have."
    res = await retrieve(query, initiative_ids or run.initiatives or None)
    run.store.add(res)
    return format_passages(res)


search_charters = tool(
    _search_charters, name="search_charters",
    description="Search the AI initiative charters. Returns ranked passages with ids, file name, section and relevance. "
                "Restrict to initiatives by id (e.g. ['INIT-06']) or pass [] to search all charters.",
    schema={"type": "object", "additionalProperties": False, "required": ["query", "initiative_ids"],
            "properties": {"query": {"type": "string", "description": "A focused search query for one aspect."},
                           "initiative_ids": {"type": "array", "items": {"type": "string"},
                                              "description": "Initiative ids to search, or [] for all."}}})


# ---------------------------------------------------------------- governed SQL
@dataclass
class SqlRun:
    store: EvidenceStore
    count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


SQL_RUN: contextvars.ContextVar[SqlRun | None] = contextvars.ContextVar("sql_run", default=None)


def _jsonable(v: Any) -> Any:
    return v if v is None or isinstance(v, (int, float, str)) else str(v)


async def _run_sql(query: str) -> str:
    run = SQL_RUN.get()
    if run is None:
        return "SQL is unavailable outside a SQL agent run."
    async with run.lock:
        if run.count >= MAX_QUERIES:
            return "Query limit reached. Write the report from the results you already have."
        run.count += 1
        qid = f"SQL-{run.count}"
    try:
        cols, rows, truncated = await asyncio.to_thread(sqldb.run_select, query)
    except sqldb.UnsafeSQLError as e:
        return f"[{qid}] REJECTED by the governance guard: {e}"
    except Exception as e:                       # syntax error, unknown column ...
        return f"[{qid}] ERROR: {type(e).__name__}: {str(e)[:300]}"
    table = sqldb.rows_to_markdown(cols, rows)
    note = f"{len(rows)} row(s)" + (f", truncated to {sqldb.MAX_ROWS}" if truncated else "")
    run.store.items[qid] = Evidence(
        id=qid, kind="sql", title="Governed value scorecard", section_path="query result", text=f"{table}\n({note})",
        sql=query.strip(), columns=cols, rows=[[_jsonable(v) for v in r] for r in rows],
        source_file="outputs/value.sqlite", found_by=[query.strip()])
    run.store.searches.append({"query": query.strip(), "confidence": "high", "top_relevance": 1.0, "ids": [qid],
                               "rows": len(rows)})
    return f"[{qid}] {note}\n{table}"


run_sql = tool(_run_sql, name="run_sql",
               description="Run one read-only SELECT over the governed scorecard views and get the result table with "
                           "its evidence id. Calculations are rejected; every number must already be in the views.",
               schema={"type": "object", "additionalProperties": False, "required": ["query"],
                       "properties": {"query": {"type": "string", "description": "A single SQLite SELECT statement."}}})
