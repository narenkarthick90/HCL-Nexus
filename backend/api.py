"""HTTP API + static frontend (FR-10).

  POST /api/ask                 {question, history?}   -> AnswerResponse (JSON)
  POST /api/ask/stream          same body              -> Server-Sent Events: progress events, then the answer
  GET  /api/scorecard           governed scorecard rows + portfolio summary (computed in code)
  GET  /api/scorecard/{id}      one initiative: values and per-metric provenance (formula + inputs)
  GET  /api/changelog           data-quality corrections and their effect
  POST /api/report/weekly       generate the weekly executive report (MD/HTML/PDF/JSON in outputs/)
  GET  /api/report/weekly       the latest generated report
  GET  /api/charters            charters in the index     POST /api/charters  multipart: file, initiative_id
  POST /api/feedback            {interaction_id, rating (+1/-1), comment?}
  GET  /api/metrics             judge scores, guard results, feedback, learned lessons
  GET  /api/config | /api/speech/token | /api/health
  GET  /files/{charters|uploads|outputs}/{name}

Budget guards: optional shared access key (APP_ACCESS_KEY), per-client rate limit, cap on concurrent pipelines.
Run locally:  uvicorn backend.api:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import settings, sqldb
from .pipeline import QAPipeline
from .pipeline import feedback as fb
from .retrieval.search import close_clients

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("openai._base_client").setLevel(logging.INFO)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")

FRONTEND = settings.ROOT / "frontend"
CHARTERS_DIR = settings.ROOT / "data" / "charters"
OUTPUTS_DIR = settings.ROOT / "outputs"
UPLOAD_DIR = settings.DATA_DIR / "uploads"
REPORT_DIR = settings.DATA_DIR / "reports"           # reports generated at runtime (writable)

ACCESS_KEY = os.getenv("APP_ACCESS_KEY", "")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_10MIN", "40"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_QUESTIONS", "3"))
SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")
SPEECH_VOICE = os.getenv("AZURE_SPEECH_VOICE", "en-US-AvaMultilingualNeural")
SPEECH_LANG = os.getenv("AZURE_SPEECH_LANGUAGE", "en-US")
UPLOADS_ENABLED = os.getenv("ENABLE_UPLOADS", "1") != "0"

state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(sqldb.ensure_db)
    from .telemetry import setup_tracing

    traced = await asyncio.to_thread(setup_tracing)
    log.info("agent runtime: %s | tracing to App Insights: %s", settings.AGENT_RUNTIME, traced)
    state["pipeline"] = QAPipeline()
    state["sem"] = asyncio.Semaphore(MAX_CONCURRENT)
    state["report_lock"] = asyncio.Lock()
    yield
    await close_clients()
    from .foundry import close as close_foundry
    from .telemetry import flush

    flush()
    await close_foundry()


app = FastAPI(title="Nimbus Group AI Value Intelligence Assistant", lifespan=lifespan)


# ---------------------------------------------------------------- guards
_hits: dict[str, deque] = defaultdict(deque)


def guard(request: Request) -> None:
    if ACCESS_KEY and request.headers.get("x-access-key") != ACCESS_KEY:
        raise HTTPException(401, "access key required")
    client = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    q, now = _hits[client], time.time()
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "rate limit reached, try again in a few minutes")
    q.append(now)


class Turn(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class FeedbackRequest(BaseModel):
    interaction_id: str
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------- Q&A
@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def config():
    inits = await asyncio.to_thread(sqldb.query_rows, "SELECT initiative_id, name FROM v_initiatives ORDER BY initiative_id")
    return {"voice": {"provider": "azure" if SPEECH_KEY and SPEECH_REGION else "browser",
                      "voice": SPEECH_VOICE, "language": SPEECH_LANG},
            "uploads": UPLOADS_ENABLED, "access_key_required": bool(ACCESS_KEY), "initiatives": inits,
            "agent_runtime": settings.AGENT_RUNTIME}


@app.post("/api/ask", dependencies=[Depends(guard)])
async def ask(req: AskRequest):
    async with state["sem"]:
        resp = await state["pipeline"].answer(req.question, [t.model_dump() for t in req.history])
    return resp.model_dump()


@app.post("/api/ask/stream", dependencies=[Depends(guard)])
async def ask_stream(req: AskRequest, request: Request):
    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            async with state["sem"]:
                await queue.put({"type": "started"})
                resp = await state["pipeline"].answer(req.question, [t.model_dump() for t in req.history],
                                                      on_event=queue.put)
            await queue.put({"type": "answer", "response": resp.model_dump()})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("pipeline failed")
            await queue.put({"type": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())

    async def events():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if ev is None:
                    break
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            if not task.done():                               # client went away (e.g. voice barge-in): stop work
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/feedback", dependencies=[Depends(guard)])
async def feedback(req: FeedbackRequest):
    inter = fb.interaction(req.interaction_id)
    if inter is None:
        raise HTTPException(404, "unknown interaction")
    fb.add_feedback(req.interaction_id, req.rating, req.comment)
    if req.rating < 0 and req.comment.strip():
        fb.learn_in_background(inter["standalone"] or inter["question"], f"User feedback: {req.comment}", "user")
    return {"ok": True}


@app.get("/api/metrics", dependencies=[Depends(guard)])
async def metrics():
    return fb.metrics()


@app.get("/api/speech/token", dependencies=[Depends(guard)])
async def speech_token():
    if not (SPEECH_KEY and SPEECH_REGION):
        raise HTTPException(404, "Azure Speech is not configured; the browser voice fallback is used")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"https://{SPEECH_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
                         headers={"Ocp-Apim-Subscription-Key": SPEECH_KEY})
    if r.status_code != 200:
        raise HTTPException(502, f"speech token request failed ({r.status_code})")
    return {"token": r.text, "region": SPEECH_REGION, "voice": SPEECH_VOICE, "language": SPEECH_LANG,
            "expires_in": 540}


# ---------------------------------------------------------------- scorecard, change log, report (read from code)
@app.get("/api/scorecard", dependencies=[Depends(guard)])
async def scorecard():
    rows = await asyncio.to_thread(sqldb.query_rows, "SELECT * FROM v_scorecard ORDER BY initiative_id")
    summary = (await asyncio.to_thread(sqldb.query_rows, "SELECT * FROM v_portfolio_summary"))[0]
    return {"rows": rows, "summary": summary}


@app.get("/api/scorecard/{initiative_id}", dependencies=[Depends(guard)])
async def scorecard_one(initiative_id: str):
    if not re.fullmatch(r"INIT-\d{2}", initiative_id):
        raise HTTPException(404)
    row = await asyncio.to_thread(sqldb.query_rows, f"SELECT * FROM v_scorecard WHERE initiative_id = '{initiative_id}'")
    if not row:
        raise HTTPException(404)
    prov = await asyncio.to_thread(sqldb.query_rows, "SELECT metric, formula, inputs, value_display FROM "
                                                     f"v_metric_provenance WHERE initiative_id = '{initiative_id}'")
    info = await asyncio.to_thread(sqldb.query_rows, f"SELECT * FROM v_initiatives WHERE initiative_id = '{initiative_id}'")
    changes = await asyncio.to_thread(sqldb.query_rows, "SELECT id, file, kind, before, after, effect FROM v_change_log "
                                                        f"WHERE initiative LIKE '%{initiative_id}%'")
    return {"row": row[0], "provenance": prov, "initiative": info[0] if info else {}, "changes": changes}


@app.get("/api/changelog", dependencies=[Depends(guard)])
async def changelog():
    return await asyncio.to_thread(sqldb.query_rows, "SELECT * FROM v_change_log")


@app.post("/api/report/weekly", dependencies=[Depends(guard)])
async def weekly_report():
    from .report import generate

    if state["report_lock"].locked():
        raise HTTPException(409, "a report is already being generated")
    async with state["report_lock"]:
        r = await generate(REPORT_DIR)
    state["latest_report"] = r
    return r


@app.get("/api/report/weekly", dependencies=[Depends(guard)])
async def latest_report():
    if "latest_report" in state:
        return state["latest_report"]
    for d in (REPORT_DIR, OUTPUTS_DIR):
        found = sorted(d.glob("weekly_report_*.json")) if d.exists() else []
        if found:
            r = json.loads(found[-1].read_text("utf-8"))
            from .report import to_html, to_markdown

            r["markdown"] = to_markdown(r)
            r["html"] = to_html(r["markdown"])
            area = "reports" if d == REPORT_DIR else "outputs"
            r["files"] = {ext: f"/files/{area}/{found[-1].stem}.{ext}" for ext in ("md", "html", "pdf", "json")}
            return r
    raise HTTPException(404, "no report generated yet")


# ---------------------------------------------------------------- charters
def _list_charters() -> list[dict]:
    out = []
    mf = CHARTERS_DIR / "manifest.yaml"
    if mf.exists():
        for m in yaml.safe_load(mf.read_text("utf-8")) or []:
            out.append({"initiative_id": m["initiative_id"], "file": m["file"], "format": m["format"],
                        "sections": m["sections"], "url": f"/files/charters/{m['file']}", "source": "seed"})
    if UPLOAD_DIR.exists():
        for meta in sorted(UPLOAD_DIR.glob("*.json")):
            out.append(json.loads(meta.read_text("utf-8")) | {"source": "upload"})
    return out


@app.get("/api/charters", dependencies=[Depends(guard)])
async def charters():
    return _list_charters()


@app.post("/api/charters", dependencies=[Depends(guard)])
async def upload(file: UploadFile = File(...), initiative_id: str = Form(...)):
    from ingest.incremental import MAX_BYTES, ingest_file

    if not UPLOADS_ENABLED:
        raise HTTPException(403, "uploads are disabled")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "upload").name)[:120]
    if Path(name).suffix.lower() not in (".pdf", ".docx", ".html", ".htm", ".md", ".txt"):
        raise HTTPException(400, "supported formats: PDF, DOCX, HTML, Markdown, TXT")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file larger than 10 MB")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / name
    path.write_bytes(data)
    try:
        result = await asyncio.to_thread(ingest_file, path, initiative_id)
    except ValueError as e:
        path.unlink(missing_ok=True)
        raise HTTPException(400, str(e)) from None
    meta = {"initiative_id": initiative_id, "file": name, "format": path.suffix.lstrip("."),
            "sections": result["sections"], "url": f"/files/uploads/{name}",
            "flagged_passages": result["flagged_passages"]}
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(meta), "utf-8")
    return result


@app.get("/files/{area}/{name}")
async def files(area: str, name: str):
    base = {"charters": CHARTERS_DIR, "uploads": UPLOAD_DIR, "outputs": OUTPUTS_DIR, "reports": REPORT_DIR}.get(area)
    if base is None or "/" in name or "\\" in name or ".." in name or name.endswith(".sqlite"):
        raise HTTPException(404)
    path = (base / name).resolve()
    if not path.is_file() or base.resolve() != path.parent:
        raise HTTPException(404)
    return FileResponse(path)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
