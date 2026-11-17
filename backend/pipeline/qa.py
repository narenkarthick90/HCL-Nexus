"""End-to-end question answering.

  router (Foundry agent)            numeric | narrative | mixed | pii_refusal | not_in_data | clarify | greeting
    -> SQL agent / Charter agent    in parallel, as routed; evidence ids only
    -> writer                       grounded draft with [S#]/[C#] labels
    -> guard (code)                 numbers, statuses, citations verified against the governed scorecard
    -> judge (LLM, gate in code)    pass | revise (max MAX_REVISIONS)
    -> final guard with strip       any sentence that still fails is removed; nothing left -> the fallback
JUDGE_MODE=auto (default) skips the LLM judge for numeric-only answers whose every number the guard verified;
JUDGE_MODE=always runs it every time.

CLI:  python -m backend.pipeline.qa "Which initiatives need attention and why?"
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any, Awaitable, Callable

from ..agents.charter_agent import CharterAgent
from ..agents.router import RouteDecision, route
from ..agents.sql_agent import SQLAgent
from ..llm import usage_scope
from ..retrieval.models import Evidence, confidence_for
from . import feedback, guard
from .judge import judge, skipped_verdict
from .models import (FALLBACK, PII_REFUSAL, AnswerResponse, Citation, ClosestMatch, Confidence, GuardReport,
                     Verdict)
from .writer import label_evidence, write_answer

MAX_REVISIONS = 2
JUDGE_MODE = os.getenv("JUDGE_MODE", "auto").lower()
GREETING = ("I answer questions about the Nimbus Group AI portfolio: adoption, value (ROI, benefit, payback, "
            "run-rate versus plan), Green/Amber/Red status, and what the initiative charters say, always with sources. "
            "Try \"Which initiatives need attention and why?\"")
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _notes(results) -> str:
    lines = []
    for r in results:
        lines += [f"- {r.agent}: {f.statement} {f.evidence_ids}" for f in r.report.findings]
        lines += [f"- {r.agent} could not find: {m}" for m in r.report.missing]
    return "\n".join(lines)[:4000]


MAX_ATTACH_INITIATIVES = 10


async def attach_sections(evidence: list[Evidence], sections: list[str]) -> list[Evidence]:
    """Deterministic charter context: the given sections for every initiative a scorecard result returned."""
    from ..retrieval.search import section_lookup

    ids: list[str] = []
    for e in evidence:
        if e.kind == "sql" and "initiative_id" in e.columns:
            col = e.columns.index("initiative_id")
            ids += [r[col] for r in e.rows if r[col] not in ids]
    pairs = [(i, s) for i in ids[:MAX_ATTACH_INITIATIVES] for s in sections]
    found = await asyncio.gather(*(section_lookup(i, s) for i, s in pairs), return_exceptions=True)
    return [e for f in found if not isinstance(f, BaseException) for e in f]


def _citations(answer: str, labelled: dict[str, Evidence]) -> list[Citation]:
    out = []
    for label, e in labelled.items():
        if f"[{label}]" not in answer:
            continue
        if e.kind == "sql":
            out.append(Citation(label=label, kind="sql", evidence_id=e.id, title=e.citation, section="query result",
                                source_file=e.source_file, excerpt=e.sql, query=e.sql, columns=e.columns, rows=e.rows))
        else:
            out.append(Citation(label=label, kind="charter", evidence_id=e.id, title=e.source_file,
                                section=e.section_path, initiative_id=e.initiative_id, page=e.page,
                                source_file=e.source_file, excerpt=e.excerpt, injection_flag=e.injection_flag))
    return out


def _confidence(evidence: list[Evidence], v: Verdict | None, rep: GuardReport) -> Confidence:
    rels = [e.relevance for e in evidence if e.relevance is not None]
    retrieval = confidence_for(max(rels)) if rels else "none"
    if v is None:
        return Confidence(label="none", retrieval=retrieval)
    if v.action == "pass" and rep.passed and v.completeness >= 4:
        label, reason = ("high", "every number verified against the scorecard; passed review") \
            if retrieval == "high" else ("medium", "passed review; some supporting passages are weak")
    elif rep.passed:
        label, reason = "medium", "numbers verified; review asked for improvements: " + "; ".join(v.missing_aspects[:2])
    else:
        label, reason = "low", "unverified statements were removed: " + "; ".join(x.detail for x in rep.violations[:2])
    return Confidence(label=label, retrieval=retrieval, groundedness=v.groundedness, relevance=v.relevance,
                      completeness=v.completeness, reason=reason)


class QAPipeline:
    def __init__(self) -> None:
        self.sql, self.charter = SQLAgent(), CharterAgent()

    async def answer(self, question: str, history: list[dict[str, str]] | None = None,
                     on_event: EventCallback | None = None) -> AnswerResponse:
        """One question = one trace tree in Foundry (router -> agents -> writer -> judge)."""
        from ..telemetry import span

        with span("nimbus.qa", question_chars=len(question), has_history=bool(history)) as s:
            resp = await self._answer(question, history, on_event)
            s.set_attribute("nimbus.response_type", resp.type)
            s.set_attribute("nimbus.route", resp.route)
            s.set_attribute("nimbus.revisions", resp.trace.get("revisions", 0))
            s.set_attribute("nimbus.guard.passed", resp.guardrails.passed)
            s.set_attribute("nimbus.guard.numbers_verified", resp.guardrails.numbers_verified)
            s.set_attribute("nimbus.interaction_id", resp.interaction_id)
            if resp.confidence:
                s.set_attribute("nimbus.confidence", resp.confidence.label)
            return resp

    async def run_agents(self, d: RouteDecision, emit) -> tuple[list, dict[str, str]]:
        async def run(t):
            await emit({"type": "agent_start", "agent": t.agent, "task": t.task})
            agent = self.sql if t.agent == "sql" else self.charter
            res = await agent.run(t.task, context=d.standalone_question, initiatives=t.initiatives or d.initiatives)
            await emit({"type": "agent_done", "agent": t.agent, "coverage": res.report.coverage,
                        "evidence": len(res.evidence), "elapsed_ms": res.elapsed_ms})
            return res

        done = await asyncio.gather(*(run(t) for t in d.tasks), return_exceptions=True)
        results, errors = [], {}
        for t, r in zip(d.tasks, done):
            if isinstance(r, BaseException):
                errors[f"{t.agent}: {t.task}"] = f"{type(r).__name__}: {str(r)[:300]}"
                await emit({"type": "agent_error", "agent": t.agent})
            else:
                results.append(r)
        return results, errors

    async def _answer(self, question: str, history: list[dict[str, str]] | None = None,
                      on_event: EventCallback | None = None) -> AnswerResponse:
        async def emit(ev: dict[str, Any]) -> None:
            if on_event:
                r = on_event(ev)
                if inspect.isawaitable(r):
                    await r

        iid = feedback.new_id()
        t0 = time.perf_counter()
        timings: dict[str, int] = {}
        verdicts: list[Verdict] = []
        revisions = 0
        results, errors, lessons = [], {}, []
        with usage_scope() as usage:
            d = await route(question, history)
            timings["route"] = int((time.perf_counter() - t0) * 1000)
            await emit({"type": "plan", "route": d.route, "standalone_question": d.standalone_question,
                        "agents": [t.agent for t in d.tasks], "initiatives": d.initiatives})
            resp = AnswerResponse(interaction_id=iid, type="answer", question=question,
                                  standalone_question=d.standalone_question, route=d.route)

            if d.route == "pii_refusal":
                resp.type, resp.answer_markdown, resp.spoken_summary = "refusal", PII_REFUSAL, PII_REFUSAL
            elif d.route == "not_in_data":
                resp.type, resp.answer_markdown, resp.spoken_summary = "not_found", FALLBACK, FALLBACK
            elif d.route == "clarify":
                resp.type, resp.answer_markdown = "clarify", d.clarifying_question or "Which initiative do you mean?"
                resp.spoken_summary, resp.clarification_options = resp.answer_markdown, d.clarification_options
            elif d.route == "greeting":
                resp.type, resp.answer_markdown, resp.spoken_summary = "greeting", GREETING, GREETING
            else:
                lessons_task = asyncio.create_task(feedback.relevant_lessons(d.standalone_question))
                t = time.perf_counter()
                results, errors = await self.run_agents(d, emit)
                timings["agents"] = int((time.perf_counter() - t) * 1000)
                evidence, seen, retrieved = [], set(), []
                for r in sorted(results, key=lambda r: r.agent != "sql"):
                    for e in r.evidence:
                        if e.id not in seen:
                            seen.add(e.id); evidence.append(e)
                    retrieved += list(r.retrieved.values())
                if d.charter_sections:
                    t = time.perf_counter()
                    attached = await attach_sections(evidence, d.charter_sections)
                    for e in attached:
                        if e.id not in seen:
                            seen.add(e.id); evidence.append(e); retrieved.append(e)
                    timings["attach_sections"] = int((time.perf_counter() - t) * 1000)
                    await emit({"type": "agent_done", "agent": "charter-sections", "coverage": "full",
                                "evidence": len(attached), "elapsed_ms": timings["attach_sections"]})
                if not evidence:
                    lessons_task.cancel()
                    if errors and not results:
                        resp.type = "error"
                        resp.answer_markdown = "Sorry, I couldn't reach the data just now. Please try again in a moment."
                        resp.spoken_summary = "Sorry, something went wrong. Please try again."
                    else:
                        resp.type, resp.answer_markdown, resp.spoken_summary = "not_found", FALLBACK, FALLBACK
                        resp.unanswered = [m for r in results for m in r.report.missing][:3]
                        related = [e for r in results for e in getattr(r, "related", [])]
                        resp.closest_matches = [ClosestMatch(source_file=e.source_file, section=e.section_path,
                                                             excerpt=e.excerpt) for e in related[:3]]
                    resp.guardrails = guard.check("", {}, retrieved=retrieved)[0]
                else:
                    try:
                        lessons = await lessons_task
                    except Exception:
                        lessons = []
                    labelled = label_evidence(evidence)
                    notes = _notes(results)
                    t = time.perf_counter()
                    draft = await write_answer(d.standalone_question, labelled, notes=notes, lessons=lessons)
                    timings["write"] = int((time.perf_counter() - t) * 1000)
                    timings["judge"] = 0
                    await emit({"type": "draft", "revision": 0})
                    while True:
                        rep, _ = guard.check(draft["answer_markdown"], labelled, question, retrieved=retrieved)
                        await emit({"type": "guard", "passed": rep.passed, "numbers_verified": rep.numbers_verified,
                                    "numbers_checked": rep.numbers_checked})
                        if draft["answer_type"] == "not_found":
                            v = skipped_verdict()
                        elif JUDGE_MODE == "auto" and d.route == "numeric" and rep.passed and not rep.violations:
                            v = skipped_verdict()
                        else:
                            t = time.perf_counter()
                            v = await judge(d.standalone_question, labelled, draft, rep)
                            timings["judge"] += int((time.perf_counter() - t) * 1000)
                        verdicts.append(v)
                        await emit({"type": "verdict", "action": v.action, "groundedness": v.groundedness,
                                    "relevance": v.relevance, "completeness": v.completeness, "skipped": v.skipped})
                        if v.action == "pass" or revisions >= MAX_REVISIONS:
                            break
                        revisions += 1
                        t = time.perf_counter()
                        draft = await write_answer(d.standalone_question, labelled, notes=notes, lessons=lessons,
                                                   previous=draft, critique=v.critique)
                        timings["write"] += int((time.perf_counter() - t) * 1000)
                        await emit({"type": "draft", "revision": revisions})
                    rep, text = guard.check(draft["answer_markdown"], labelled, question, strip=True,
                                            retrieved=retrieved)
                    resp.guardrails = rep
                    resp.answer_markdown, resp.format = text, draft.get("format", "prose")
                    resp.type = "not_found" if text == FALLBACK else "answer"
                    resp.spoken_summary = FALLBACK if text == FALLBACK else (
                        draft["spoken_summary"] if not rep.removed_sentences else _first_sentence(text))
                    resp.unanswered = draft.get("unanswered", [])
                    resp.citations = _citations(text, labelled)
                    resp.confidence = _confidence(evidence, verdicts[-1], rep)

        timings["total"] = int((time.perf_counter() - t0) * 1000)
        resp.trace = {
            "route": d.model_dump(),
            "agents": [{"agent": r.agent, "task": r.task, "coverage": r.report.coverage, "elapsed_ms": r.elapsed_ms,
                        "queries": [q["query"] for q in r.queries], "missing": r.report.missing,
                        "injection_notes": r.report.injection_notes} for r in results],
            "agent_errors": errors, "verdicts": [v.model_dump() for v in verdicts], "revisions": revisions,
            "lessons_used": lessons, "timings_ms": timings, "usage": usage.summary(),
        }
        if verdicts and verdicts[0].action == "revise" and verdicts[0].critique and not verdicts[0].skipped:
            feedback.learn_in_background(d.standalone_question, verdicts[0].critique, "judge")
        final = verdicts[-1] if verdicts else None
        feedback.log_interaction({
            "id": iid, "question": question, "standalone": d.standalone_question, "type": resp.type,
            "answer": resp.answer_markdown, "verdicts": resp.trace["verdicts"], "revisions": revisions,
            "retrievals": 0, "latency_ms": timings["total"], "cost_usd": usage.cost_usd(),
            "final_scores": {"groundedness": final.groundedness, "relevance": final.relevance,
                             "completeness": final.completeness} if final else {}})
        await emit({"type": "done", "interaction_id": iid, "elapsed_ms": timings["total"]})
        return resp


def _first_sentence(text: str) -> str:
    import re

    plain = re.sub(r"\[(?:S|C)\d+\]|[*_`|#]", "", text).strip()
    return re.split(r"(?<=[.!?])\s", plain, maxsplit=1)[0][:300]


async def _cli() -> None:
    import sys

    from ..foundry import close as close_foundry
    from ..retrieval.search import close_clients
    from ..telemetry import flush, setup_tracing

    setup_tracing()
    events: list[str] = []
    r = await QAPipeline().answer(" ".join(sys.argv[1:]), on_event=lambda e: events.append(
        e["type"] + (f":{e['agent']}" if "agent" in e else "") + (f":{e['action']}" if "action" in e else "")))
    await asyncio.sleep(0.1)
    await asyncio.gather(*feedback._background, return_exceptions=True)
    await close_clients()
    flush()
    await close_foundry()
    print("events:", events)
    print(f"\n[{r.type}] route={r.route} confidence={r.confidence.label if r.confidence else '-'} "
          f"({r.confidence.reason if r.confidence else ''})\n\n{r.answer_markdown}\n")
    for c in r.citations:
        print(f"[{c.label}] {c.title} > {c.section}" + (f"  SQL: {c.query[:120]}" if c.query else ""))
    g = r.guardrails
    print(f"\nguard: passed={g.passed} numbers {g.numbers_verified}/{g.numbers_checked} verified, "
          f"charter figures {g.charter_figures}, removed {len(g.removed_sentences)}")
    for v in g.violations:
        print(f"   {v.kind}: {v.detail}")
    for n in g.notes:
        print(f"   note: {n}")
    t = r.trace
    print(f"verdicts: {[(v['action'], v['groundedness'], v['relevance'], v['completeness'], v['skipped']) for v in t['verdicts']]}"
          f" revisions={t['revisions']}\ntimings: {t['timings_ms']}\ncost: ${t['usage']['cost_usd']:.4f}")


if __name__ == "__main__":
    asyncio.run(_cli())
