"""Persistent feedback loop.

Every interaction is logged with its judge verdicts. Two sources produce *lessons*:
  - the judge: when a first draft failed review, the critique is distilled into a short reusable rule
  - users: a thumbs-down with a comment is distilled the same way
Lessons are embedded; for each new question the most similar lessons (cosine >= MIN_SIM) are injected into the
writer prompt, so recurring mistakes are corrected up-front instead of costing a revision round.

Storage: SQLite in APP_DATA_DIR (a mounted Azure Files share in Container Apps keeps it across restarts).
Distillation runs in the background after the response is returned, so it adds no latency.
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import time
import uuid
from typing import Any

from .. import settings
from ..llm import embed, json_completion, strict_schema

DB = settings.DATA_DIR / "feedback.sqlite"
MIN_SIM = 0.45
DUP_SIM = 0.92
MAX_LESSONS = 3

_lock = threading.Lock()
_con: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _con
    if _con is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(DB, check_same_thread=False)
        _con.executescript("""
            CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY, ts REAL, question TEXT, standalone TEXT, type TEXT, answer TEXT,
                verdicts TEXT, revisions INTEGER, retrievals INTEGER, final_scores TEXT, latency_ms INTEGER,
                cost_usd REAL);
            CREATE TABLE IF NOT EXISTS feedback (
                interaction_id TEXT, ts REAL, rating INTEGER, comment TEXT);
            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY, ts REAL, source TEXT, question TEXT, lesson TEXT, embedding TEXT,
                uses INTEGER DEFAULT 0, active INTEGER DEFAULT 1);""")
    return _con


def _exec(sql: str, args: tuple = ()) -> list[tuple]:
    with _lock:
        cur = _db().execute(sql, args)
        rows = cur.fetchall()
        _db().commit()
        return rows


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def log_interaction(r: dict[str, Any]) -> None:
    _exec("INSERT OR REPLACE INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (r["id"], time.time(), r["question"], r.get("standalone", ""), r["type"], r.get("answer", ""),
           json.dumps(r.get("verdicts", [])), r.get("revisions", 0), r.get("retrievals", 0),
           json.dumps(r.get("final_scores", {})), r.get("latency_ms", 0), r.get("cost_usd", 0.0)))


def add_feedback(interaction_id: str, rating: int, comment: str = "") -> None:
    _exec("INSERT INTO feedback VALUES (?,?,?,?)", (interaction_id, time.time(), rating, comment))


# ---------------------------------------------------------------- lessons
def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1.0)


async def relevant_lessons(question: str, k: int = MAX_LESSONS) -> list[str]:
    rows = _exec("SELECT id, lesson, embedding FROM lessons WHERE active = 1")
    if not rows:
        return []
    q = (await embed([question]))[0]
    scored = sorted(((_cos(q, json.loads(e)), i, l) for i, l, e in rows), reverse=True)
    picked = [(i, l) for s, i, l in scored[:k] if s >= MIN_SIM]
    for i, _ in picked:
        _exec("UPDATE lessons SET uses = uses + 1 WHERE id = ?", (i,))
    return [l for _, l in picked]


DISTIL = """You turn a reviewer's critique of an answer into ONE short, reusable instruction for the answer writer \
(max 30 words). It must generalise to similar future questions, must not contain facts that could become wrong \
(no specific numbers or dates unless they describe a document version to check), and must be actionable. \
If the critique is trivial or too specific to reuse, return an empty string."""

_DISTIL_SCHEMA = strict_schema({"lesson": {"type": "string"}})


async def distil_lesson(question: str, critique: str, source: str) -> str | None:
    data = await json_completion(DISTIL, f"Question: {question}\nCritique: {critique}", "lesson", _DISTIL_SCHEMA,
                                 role="judge", max_tokens=400)
    lesson = data.get("lesson", "").strip()
    if not lesson:
        return None
    vec = (await embed([lesson]))[0]
    for (e,) in _exec("SELECT embedding FROM lessons WHERE active = 1"):
        if _cos(vec, json.loads(e)) >= DUP_SIM:
            return None                                      # near-duplicate of an existing lesson
    _exec("INSERT INTO lessons (id, ts, source, question, lesson, embedding) VALUES (?,?,?,?,?,?)",
          (new_id(), time.time(), source, question, lesson, json.dumps(vec)))
    return lesson


_background: set[asyncio.Task] = set()


def learn_in_background(question: str, critique: str, source: str) -> None:
    async def _run() -> None:
        try:
            await distil_lesson(question, critique, source)
        except Exception as e:                                # never break a request because of learning
            print(f"[feedback] lesson distillation failed: {type(e).__name__}: {e}")

    t = asyncio.create_task(_run())
    _background.add(t)
    t.add_done_callback(_background.discard)


def interaction(interaction_id: str) -> dict | None:
    rows = _exec("SELECT question, standalone FROM interactions WHERE id = ?", (interaction_id,))
    return {"question": rows[0][0], "standalone": rows[0][1]} if rows else None


def metrics() -> dict[str, Any]:
    n, = _exec("SELECT COUNT(*) FROM interactions")[0]
    rows = _exec("SELECT type, COUNT(*), AVG(latency_ms), AVG(revisions), SUM(cost_usd) FROM interactions GROUP BY type")
    scores = [json.loads(s) for (s,) in _exec("SELECT final_scores FROM interactions WHERE type='answer'") if s]
    avg = {k: round(sum(s.get(k, 0) for s in scores) / len(scores), 2) for k in ("groundedness", "relevance",
                                                                                   "completeness")} if scores else {}
    first_pass = _exec("SELECT COUNT(*) FROM interactions WHERE type='answer' AND revisions = 0")[0][0]
    fb = _exec("SELECT SUM(rating > 0), SUM(rating < 0) FROM feedback")[0]
    lessons = _exec("SELECT source, lesson, uses FROM lessons WHERE active = 1 ORDER BY ts DESC LIMIT 20")
    return {"interactions": n,
            "by_type": {t: {"count": c, "avg_latency_ms": int(l or 0), "avg_revisions": round(r or 0, 2),
                            "cost_usd": round(cst or 0, 4)} for t, c, l, r, cst in rows},
            "avg_final_scores": avg, "first_draft_pass_rate": round(first_pass / len(scores), 2) if scores else None,
            "feedback": {"up": fb[0] or 0, "down": fb[1] or 0},
            "lessons": [{"source": s, "lesson": l, "uses": u} for s, l, u in lessons]}
