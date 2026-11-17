"""OpenTelemetry tracing to the Foundry project's Application Insights (ai.azure.com > Traces).

Agent Framework emits GenAI spans for every agent run, model call and tool call; this module exports them to the
App Insights resource connected to the Foundry project. Each question is wrapped in one parent span (`nimbus.qa`),
so a request shows as a single trace tree: plan -> sub-agents (+ tool calls) -> writer -> judge.

The connection string comes from APPLICATIONINSIGHTS_CONNECTION_STRING, or is fetched from the Foundry project.
Prompt/answer content is NOT recorded unless TRACE_CONTENT=1.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from . import settings

log = logging.getLogger("telemetry")
_configured = False


def connection_string() -> str | None:
    cs = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if cs:
        return cs
    if settings.FOUNDRY_PROJECT_ENDPOINT:
        try:
            from .foundry import project_client

            return project_client().telemetry.get_application_insights_connection_string()
        except Exception as e:
            log.warning("could not read the App Insights connection string from the project: %s", e)
    return None


def setup_tracing(service_name: str = "nimbus-docs-assistant") -> bool:
    """Idempotent. Returns True when traces are exported."""
    global _configured
    if _configured or not settings.TRACING_ENABLED:
        return _configured
    cs = connection_string()
    if not cs:
        return False
    from agent_framework.observability import configure_otel_providers
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

    configure_otel_providers(service_name=service_name, exporters=[AzureMonitorTraceExporter(connection_string=cs)],
                             enable_sensitive_data=settings.TRACE_CONTENT)
    _configured = True
    log.info("tracing to Application Insights enabled (content recording: %s)", settings.TRACE_CONTENT)
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A parent span for one request; a no-op when tracing is off."""
    from opentelemetry import trace

    with trace.get_tracer("nimbus").start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else str(v))
        yield s


def flush() -> None:
    """Export pending spans (call before a CLI or batch job exits)."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(10_000)
    except Exception:
        pass
