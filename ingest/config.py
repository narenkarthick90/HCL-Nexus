"""Shared configuration for the ingestion pipeline. Values come from environment variables / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DOCS_DIR = ROOT / "data" / "charters"
OUT_DIR = ROOT / "ingest" / "out"

# One index for the initiative charters (filter by initiative_id).
INDEXES: dict[str, str] = {"charters": "charters"}
SYNONYM_MAPS: dict[str, str] = {d: f"syn-{d}" for d in INDEXES}

EMBEDDING_DIMS = 1536            # text-embedding-3-small

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "")
AZURE_SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY", "")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")


def azure_configured() -> bool:
    return bool(AZURE_SEARCH_ENDPOINT and AZURE_OPENAI_ENDPOINT)
