"""Retrieval layer: query expansion -> hybrid search (BM25 + vectors) -> semantic or LLM rerank."""
from .models import Evidence, RetrievalResult  # noqa: F401
from .search import retrieve  # noqa: F401
