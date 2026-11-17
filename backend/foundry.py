"""Microsoft Foundry Agent Service integration.

The 5 agents (router, sql, charter, writer, judge) are defined once (instructions, structured output, reasoning effort, tools) and either
  - published to the Foundry project as versioned prompt agents and run through Foundry Agent Service
    (AGENT_RUNTIME=foundry: visible and traceable in ai.azure.com > Agents), or
  - run in-process from the same definition (AGENT_RUNTIME=local: tests, offline fallback).

  python -m backend.foundry publish [--force]   # create a new version for every agent whose definition changed
  python -m backend.foundry list                # show agents and versions in the project
  python -m backend.foundry delete <name>       # delete one agent (all versions)

Auth is Microsoft Entra ID: the container's managed identity in Azure Container Apps, the az CLI login locally.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from . import settings
from .llm import agent_chat_client, agent_options, record_agent_usage


class _DropToolDeclarationNotice(logging.Filter):
    """FoundryAgent warns that local tool declarations are not sent; that is exactly the intended setup."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "tool declarations cannot be sent" not in record.getMessage()


logging.getLogger("agent_framework.foundry").addFilter(_DropToolDeclarationNotice())


@dataclass(frozen=True)
class AgentSpec:
    key: str                       # router | sql | charter | writer | judge
    role: str                      # settings role: model + reasoning effort
    title: str                     # display name used in instructions
    description: str
    instructions: str
    output: type[BaseModel]
    tools: tuple = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return f"{settings.AGENT_NAME_PREFIX}{self.key}"


@lru_cache(maxsize=1)
def specs() -> dict[str, AgentSpec]:
    from . import sqldb
    from .agents import charter_agent, router, sql_agent, tools
    from .agents.common import AgentReport
    from .pipeline import judge, writer

    return {
        "router": AgentSpec("router", "planner", "RouterAgent",
                            "Routes each question: metrics to the scorecard (SQL agent), narrative to the charters "
                            "(Charter agent), mixed to both; stops personal-data and out-of-data requests.",
                            router.instructions(), router.RouteDecision),
        "sql": AgentSpec("sql", "sql", "SQLAgent", sql_agent.DESCRIPTION,
                         sql_agent.INSTRUCTIONS.format(schema=sqldb.schema_description(), max_q=tools.MAX_QUERIES),
                         AgentReport, (tools.run_sql,)),
        "charter": AgentSpec("charter", "subagent", "CharterAgent", charter_agent.DESCRIPTION,
                             charter_agent.INSTRUCTIONS, AgentReport, (tools.search_charters,)),
        "writer": AgentSpec("writer", "writer", "WriterAgent",
                            "Writes concise, cited answers and weekly-report narrative using only scorecard values "
                            "and charter passages.", writer.SYSTEM + f"\n[{writer.PROMPT_VERSION}]", writer.WriterOutput),
        "judge": AgentSpec("judge", "judge", "JudgeAgent",
                           "Real-time LLM-as-a-judge: scores groundedness, relevance and completeness; the pass gate "
                           "and the number guard are enforced in code.",
                           judge.SYSTEM + f"\n[{judge.PROMPT_VERSION}]", judge.JudgeOutput),
    }


# ---------------------------------------------------------------- credentials and clients
def _in_azure() -> bool:
    return bool(os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"))


@lru_cache(maxsize=1)
def sync_credential():
    from azure.identity import AzureCliCredential, ManagedIdentityCredential

    return ManagedIdentityCredential() if _in_azure() else AzureCliCredential(process_timeout=30)


_async_cred = None


def async_credential():
    global _async_cred
    if _async_cred is None:
        from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

        _async_cred = ManagedIdentityCredential() if _in_azure() else AzureCliCredential(process_timeout=30)
    return _async_cred


@lru_cache(maxsize=1)
def project_client():
    from azure.ai.projects import AIProjectClient

    if not settings.FOUNDRY_PROJECT_ENDPOINT:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set")
    return AIProjectClient(endpoint=settings.FOUNDRY_PROJECT_ENDPOINT, credential=sync_credential())


# ---------------------------------------------------------------- definitions + publishing
def build_agent(spec: AgentSpec, foundry: bool):
    """The Agent Framework Agent for a spec: FoundryChatClient for publishing, the v1 chat client locally."""
    from agent_framework import Agent

    model, effort = settings.role_model(spec.role)
    if foundry:
        from agent_framework.foundry import FoundryChatClient

        client = FoundryChatClient(project_endpoint=settings.FOUNDRY_PROJECT_ENDPOINT, model=model,
                                   credential=async_credential())
        opts: dict[str, Any] = {"response_format": spec.output}
        if effort:
            opts["reasoning"] = {"effort": effort}
        return Agent(client, name=spec.name, description=spec.description, instructions=spec.instructions,
                     tools=list(spec.tools), default_options=opts)
    return Agent(agent_chat_client(spec.role), name=spec.title, description=spec.description,
                 instructions=spec.instructions, tools=list(spec.tools))


def definition(spec: AgentSpec):
    from agent_framework.foundry import to_prompt_agent

    return to_prompt_agent(build_agent(spec, foundry=True))


def definition_hash(defn) -> str:
    return hashlib.sha256(json.dumps(defn.as_dict(), sort_keys=True, default=str).encode()).hexdigest()[:16]


def _latest(name: str):
    """Latest version details of an agent, or None if it does not exist."""
    try:
        versions = list(project_client().agents.list_versions(agent_name=name, limit=1, order="desc"))
    except Exception:
        return None
    return versions[0] if versions else None


def publish(force: bool = False) -> dict[str, str]:
    result = {}
    for spec in specs().values():
        defn = definition(spec)
        h = definition_hash(defn)
        latest = _latest(spec.name)
        if latest is not None and not force and (latest.metadata or {}).get("definition_hash") == h:
            result[spec.name] = f"v{latest.version} (unchanged)"
            continue
        created = project_client().agents.create_version(
            agent_name=spec.name, definition=defn, description=spec.description,
            metadata={"definition_hash": h, "app": "nimbus-value-intelligence", "role": spec.key})
        result[spec.name] = f"v{created.version} (published)"
    return result


# ---------------------------------------------------------------- runtime
# Per-request timeout for Foundry Agent Service calls. The SDK default is 600 s, and the endpoint occasionally
# stalls a request; failing fast lets the client's automatic retry recover (normal calls take 3-15 s).
# Per agent, ~2.5x its normal duration (measured: planner ~6 s, writer ~6 s, judge ~5 s, sub-agents 8-15 s).
AGENT_TIMEOUT_S = {"router": 25.0, "writer": 40.0, "judge": 30.0, "sql": 45.0, "charter": 45.0}
if os.getenv("AGENT_TIMEOUT_S"):
    AGENT_TIMEOUT_S = {k: float(os.environ["AGENT_TIMEOUT_S"]) for k in AGENT_TIMEOUT_S}
_remote: dict[str, Any] = {}
_local: dict[str, Any] = {}
_remote_lock = asyncio.Lock()


async def _remote_agent(spec: AgentSpec):
    if spec.key not in _remote:
        async with _remote_lock:
            if spec.key not in _remote:
                from agent_framework.foundry import FoundryAgent

                latest = await asyncio.to_thread(_latest, spec.name)
                if latest is None:
                    raise RuntimeError(f"Foundry agent {spec.name!r} not found; run: python -m backend.foundry publish")
                _remote[spec.key] = FoundryAgent(project_endpoint=settings.FOUNDRY_PROJECT_ENDPOINT,
                                                 agent_name=spec.name, agent_version=str(latest.version),
                                                 credential=async_credential(), tools=list(spec.tools),
                                                 timeout=AGENT_TIMEOUT_S.get(spec.key, 40.0))
    return _remote[spec.key]


def _local_agent(spec: AgentSpec):
    if spec.key not in _local:
        _local[spec.key] = build_agent(spec, foundry=False)
    return _local[spec.key]


async def run_agent(key: str, prompt: str) -> tuple[BaseModel, Any]:
    """Run one of the agents and return (parsed structured output, raw response)."""
    spec = specs()[key]
    if settings.AGENT_RUNTIME == "foundry":
        resp = await (await _remote_agent(spec)).run(prompt)         # the registered agent owns its output schema
        parsed = spec.output.model_validate_json(resp.text)
    else:
        resp = await _local_agent(spec).run(prompt, options=agent_options(spec.role, response_format=spec.output))
        parsed = resp.value
    record_agent_usage(resp, spec.role)
    return parsed, resp


async def close() -> None:
    global _async_cred
    if _async_cred is not None:
        await _async_cred.close()
        _async_cred = None


# ---------------------------------------------------------------- CLI
def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--force", action="store_true")
    sub.add_parser("list")
    d = sub.add_parser("delete")
    d.add_argument("name")
    a = ap.parse_args()
    if a.cmd == "publish":
        for name, status in publish(a.force).items():
            print(f"  {name:<22} {status}")
    elif a.cmd == "list":
        for ag in project_client().agents.list():
            latest = _latest(ag.name)
            print(f"  {ag.name:<22} latest v{latest.version if latest else '?'}  "
                  f"{(latest.metadata or {}).get('definition_hash', '') if latest else ''}")
    elif a.cmd == "delete":
        project_client().agents.delete(agent_name=a.name)
        print(f"deleted {a.name}")


if __name__ == "__main__":
    _cli()
