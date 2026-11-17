"""Backend settings. Shares .env loading and Azure endpoints with the ingestion package.

Per-role models: every LLM role can use its own Foundry deployment and reasoning effort.
  MODEL_<ROLE>=<deployment name>      default: AZURE_OPENAI_CHAT_DEPLOYMENT (judge: AZURE_OPENAI_JUDGE_DEPLOYMENT)
  EFFORT_<ROLE>=minimal|low|medium|high|none|off
      "off" omits the reasoning_effort parameter entirely (required for non-reasoning models such as
      gpt-4.1-nano or gpt-4o-mini). "none" is a valid effort value on newer GPT-5.x models.
Roles: planner (router), subagent (charter), expansion, sql, writer, judge, rerank
"""
from __future__ import annotations

import os

from ingest.config import (  # noqa: F401  (re-exported)
    AZURE_OPENAI_API_KEY, AZURE_OPENAI_EMBEDDING_DEPLOYMENT, AZURE_OPENAI_ENDPOINT, AZURE_SEARCH_API_KEY,
    AZURE_SEARCH_ENDPOINT, EMBEDDING_DIMS, INDEXES, ROOT,
)

CHAT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini")
JUDGE_DEPLOYMENT = os.getenv("AZURE_OPENAI_JUDGE_DEPLOYMENT", CHAT_DEPLOYMENT)

ROLE_DEFAULT_EFFORT = {"planner": "low", "subagent": "low", "expansion": "minimal", "sql": "low",
                       "writer": "low", "judge": "low", "rerank": "minimal"}


def role_model(role: str) -> tuple[str, str | None]:
    """(deployment, reasoning_effort or None) for an LLM role."""
    default = JUDGE_DEPLOYMENT if role == "judge" else CHAT_DEPLOYMENT
    deployment = os.getenv(f"MODEL_{role.upper()}") or default
    effort = os.getenv(f"EFFORT_{role.upper()}", ROLE_DEFAULT_EFFORT.get(role, "low")).strip().lower()
    return deployment, (None if effort in ("", "off") else effort)


def effort_options(role: str) -> dict:
    """Agent Framework run options for a role (model override + reasoning effort if any)."""
    deployment, effort = role_model(role)
    opts: dict = {"model": deployment}
    if effort:
        opts["reasoning_effort"] = effort
    return opts


# Microsoft Foundry Agent Service. AGENT_RUNTIME=foundry runs the 5 agents as registered prompt agents in the
# project (visible under ai.azure.com > Agents); "local" runs the same definitions in-process (tests / fallback).
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
AGENT_RUNTIME = os.getenv("AGENT_RUNTIME", "foundry" if FOUNDRY_PROJECT_ENDPOINT else "local").lower()
AGENT_NAME_PREFIX = os.getenv("AGENT_NAME_PREFIX", "nimbus-")
TRACING_ENABLED = os.getenv("TRACING", "1") != "0"
TRACE_CONTENT = os.getenv("TRACE_CONTENT", "0") == "1"      # include prompts/answers in traces (off by default)

# Azure OpenAI v1 API (no api-version needed; supports reasoning_effort + strict JSON schema)
OPENAI_V1_BASE_URL = AZURE_OPENAI_ENDPOINT.rstrip("/") + "/openai/v1/" if AZURE_OPENAI_ENDPOINT else ""

DATA_DIR = ROOT / os.getenv("APP_DATA_DIR", ".cache")          # writable dir (cache, sqlite, feedback)
CACHE_PATH = DATA_DIR / "llm_cache.sqlite"
CACHE_ENABLED = os.getenv("LLM_CACHE", "1") != "0"

# Approximate USD per 1M tokens (input, output), only for the cost read-out. Azure's price page did not
# expose numbers when checked (2026-11-15); override or extend with PRICE_<deployment>=in,out in .env.
PRICES_PER_M = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "text-embedding-3-small": (0.02, 0.0),
}
for _k, _v in os.environ.items():
    if _k.startswith("PRICE_"):
        try:
            _i, _o = (float(x) for x in _v.split(","))
            PRICES_PER_M[_k[6:].lower().replace("_", "-")] = (_i, _o)
        except ValueError:
            pass
