"""Shared model access: OpenAI-compatible clients for Microsoft Foundry, embeddings, strict-JSON
completions, a small SQLite cache (so evaluation reruns cost nothing), and token/cost tracking."""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Generator

from openai import AsyncOpenAI

from . import settings


# ---------------------------------------------------------------- usage / cost tracking
@dataclass
class UsageTracker:
    tokens: dict[str, list[int]] = field(default_factory=dict)    # model -> [input, output]
    calls: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0

    def record(self, model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        t = self.tokens.setdefault(model, [0, 0])
        t[0] += input_tokens or 0
        t[1] += output_tokens or 0
        self.calls[model] = self.calls.get(model, 0) + 1

    @staticmethod
    def _price(model: str) -> tuple[float, float] | None:
        # longest matching prefix wins ("gpt-5-mini" must not match a "gpt-5" entry)
        keys = sorted((k for k in settings.PRICES_PER_M if model.startswith(k)), key=len, reverse=True)
        return settings.PRICES_PER_M[keys[0]] if keys else None

    def cost_usd(self) -> float:
        total = 0.0
        for model, (i, o) in self.tokens.items():
            if p := self._price(model):
                total += i / 1e6 * p[0] + o / 1e6 * p[1]
        return total

    def summary(self) -> dict[str, Any]:
        return {"tokens": {m: {"input": i, "output": o} for m, (i, o) in self.tokens.items()},
                "calls": dict(self.calls), "cache_hits": self.cache_hits, "cost_usd": round(self.cost_usd(), 6),
                "unpriced_models": [m for m in self.tokens if self._price(m) is None]}


GLOBAL_USAGE = UsageTracker()
# stack of active scopes; nested scopes (pipeline > agent) all receive every record
_scope: contextvars.ContextVar[tuple[UsageTracker, ...]] = contextvars.ContextVar("usage_scope", default=())


def record_usage(model: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
    GLOBAL_USAGE.record(model, input_tokens, output_tokens)
    for s in _scope.get():
        s.record(model, input_tokens, output_tokens)


def _record_cache_hit() -> None:
    GLOBAL_USAGE.cache_hits += 1
    for s in _scope.get():
        s.cache_hits += 1


@contextlib.contextmanager
def usage_scope() -> Generator[UsageTracker, None, None]:
    """Collect usage for one request: `with usage_scope() as u: ...; u.summary()`. Scopes nest."""
    tracker = UsageTracker()
    token = _scope.set(_scope.get() + (tracker,))
    try:
        yield tracker
    finally:
        _scope.reset(token)


# ---------------------------------------------------------------- cache
class _Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            settings.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(settings.CACHE_PATH, check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        return self._db

    def get(self, key: str) -> Any | None:
        if not settings.CACHE_ENABLED:
            return None
        with self._lock:
            row = self._conn().execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        if not settings.CACHE_ENABLED:
            return
        with self._lock:
            self._conn().execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self._conn().commit()


CACHE = _Cache()


def cache_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- clients
REQUEST_TIMEOUT_S = 30.0       # SDK default is 600 s; the endpoint occasionally stalls a request under bursts,
                               # so fail fast and retry (normal calls finish in 2-15 s)
MAX_RETRIES = 2                # 429 / 5xx retries (the SDK honours Retry-After)
MAX_CONCURRENT_LLM = 6         # keeps parallel sub-agents under a low per-minute token quota


class _LimitedTransport:
    """httpx transport wrapper that caps concurrent requests to the model endpoint."""

    def __init__(self, inner, limit: int) -> None:
        import asyncio

        self._inner, self._sem = inner, asyncio.Semaphore(limit)

    async def handle_async_request(self, request):
        async with self._sem:
            return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@lru_cache(maxsize=1)
def openai_client() -> AsyncOpenAI:
    if not settings.OPENAI_V1_BASE_URL:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not set (see .env.example)")
    import httpx

    http = httpx.AsyncClient(transport=_LimitedTransport(httpx.AsyncHTTPTransport(), MAX_CONCURRENT_LLM),
                             timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=10.0))
    return AsyncOpenAI(base_url=settings.OPENAI_V1_BASE_URL, api_key=settings.AZURE_OPENAI_API_KEY,
                       timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES, http_client=http)


def agent_chat_client(role: str = "subagent"):
    """Chat client for Microsoft Agent Framework agents (Chat Completions over the Foundry v1 API), bound to the
    role's deployment. Shares the AsyncOpenAI client above (same timeout, retries and concurrency cap)."""
    from agent_framework.openai import OpenAIChatCompletionClient

    return OpenAIChatCompletionClient(model=settings.role_model(role)[0], async_client=openai_client())


def agent_options(role: str, **extra: Any) -> dict:
    """Run options for an agent call: reasoning effort for the role (omitted for non-reasoning models)."""
    _, effort = settings.role_model(role)
    opts = dict(extra)
    if effort:
        opts["reasoning_effort"] = effort
    return opts


def record_agent_usage(response: Any, role: str = "subagent") -> None:
    u = getattr(response, "usage_details", None) or {}
    record_usage(settings.role_model(role)[0], u.get("input_token_count", 0), u.get("output_token_count", 0))


# ---------------------------------------------------------------- embeddings
async def embed(texts: list[str]) -> list[list[float]]:
    model = settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    keys = [cache_key("emb", model, settings.EMBEDDING_DIMS, t) for t in texts]
    out: list[list[float] | None] = [CACHE.get(k) for k in keys]
    todo = [i for i, v in enumerate(out) if v is None]
    if len(todo) < len(texts):
        _record_cache_hit()
    if todo:
        resp = await openai_client().embeddings.create(
            model=model, input=[texts[i].replace("\n", " ") for i in todo], dimensions=settings.EMBEDDING_DIMS)
        record_usage(model, resp.usage.prompt_tokens, 0)
        for d in resp.data:
            i = todo[d.index]
            out[i] = d.embedding
            CACHE.put(keys[i], d.embedding)
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------- strict JSON completion
async def json_completion(system: str, user: str, schema_name: str, schema: dict, *,
                          role: str = "expansion", model: str | None = None, effort: str | None = None,
                          max_tokens: int = 2000, use_cache: bool = True) -> dict:
    role_dep, role_effort = settings.role_model(role)
    model = model or role_dep
    effort = effort if effort is not None else role_effort
    key = cache_key("json", model, effort, system, user, schema)
    if use_cache and (hit := CACHE.get(key)) is not None:
        _record_cache_hit()
        return hit
    extra = {"reasoning_effort": effort} if effort else {}
    resp = await openai_client().chat.completions.create(
        model=model, max_completion_tokens=max_tokens, **extra,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": schema_name, "strict": True, "schema": schema}})
    record_usage(model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
    content = resp.choices[0].message.content or ""
    data = json.loads(content)
    if use_cache:
        CACHE.put(key, data)
    return data


def strict_schema(properties: dict[str, dict]) -> dict:
    """Build a strict JSON schema object where every property is required."""
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
