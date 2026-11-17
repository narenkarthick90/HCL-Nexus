"""Detect instructions aimed at an AI system inside charter passages (FR-08).

Primary: Azure AI Content Safety Prompt Shields (document attack detection), called on the existing
AI Services resource with its key. Fallback (no endpoint, or the call fails): a conservative pattern check.
The flag is stored on each indexed passage; flagged passages stay retrievable as evidence (they are still
charter content) but prompts wrap them as untrusted data and the assistant is told an instruction was found.
"""
from __future__ import annotations

import re

import httpx

from . import config

API = "/contentsafety/text:shieldPrompt?api-version=2024-09-01"
_PATTERNS = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|system (note|prompt|message) to the ai|"
    r"you are now|disregard (the|all) (rules|instructions)|always report|do not mention this)", re.I)
MAX_DOCS, MAX_CHARS = 5, 9000


def heuristic(text: str) -> bool:
    return bool(_PATTERNS.search(text))


def shield(texts: list[str]) -> tuple[list[bool], str]:
    """Return (flags, method). One flag per text."""
    if not (config.AZURE_OPENAI_ENDPOINT and config.AZURE_OPENAI_API_KEY):
        return [heuristic(t) for t in texts], "heuristic"
    flags: list[bool] = []
    try:
        with httpx.Client(timeout=30) as client:
            i = 0
            while i < len(texts):
                batch, size = [], 0
                while i < len(texts) and len(batch) < MAX_DOCS and size + len(texts[i][:MAX_CHARS]) <= MAX_CHARS * 2:
                    batch.append(texts[i][:MAX_CHARS]); size += len(batch[-1]); i += 1
                r = client.post(config.AZURE_OPENAI_ENDPOINT.rstrip("/") + API,
                                headers={"Ocp-Apim-Subscription-Key": config.AZURE_OPENAI_API_KEY},
                                json={"userPrompt": "Summarise this charter.", "documents": batch})
                r.raise_for_status()
                flags += [d["attackDetected"] for d in r.json()["documentsAnalysis"]]
        # the pattern check can only add flags (belt and braces), never remove a Prompt Shields flag
        return [f or heuristic(t) for f, t in zip(flags, texts)], "prompt-shields"
    except Exception as e:  # noqa: BLE001
        print(f"  Prompt Shields unavailable ({type(e).__name__}); using the pattern check")
        return [heuristic(t) for t in texts], "heuristic"
