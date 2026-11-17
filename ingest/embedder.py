"""Embedding providers.

AzureOpenAIEmbedder  - text-embedding-3-small through Microsoft Foundry / Azure OpenAI (used for indexing).
HashEmbedder         - offline, deterministic bag-of-words vectors. Zero cost. Used for dry runs and tests,
                       and as the fallback for the semantic splitter when Azure is not configured.
"""
from __future__ import annotations

import math
import re
import zlib
from typing import Protocol

from . import config

_STOP = set("""a an the and or of to in on for with by at from as is are was were be been it its this that these
those into than then so such not no do does did can may must should will would their there here which who whom
whose what when where how all any each per via up out over under about after before between within without
if but also only more most other same own you your we our they them he she his her i me my""".split())


class Embedder(Protocol):
    name: str
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    name = "hash"

    def __init__(self, dims: int = 512) -> None:
        self.dims = dims

    def _vec(self, text: str) -> list[float]:
        toks = [t for t in re.findall(r"[a-z0-9][a-z0-9'\-]*", text.lower()) if t not in _STOP]
        feats = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
        v = [0.0] * self.dims
        for f in feats:
            h = zlib.crc32(f.encode())
            v[h % self.dims] += 1.0 if (h >> 31) else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class AzureOpenAIEmbedder:
    name = "azure-openai"

    def __init__(self, deployment: str | None = None, dims: int = config.EMBEDDING_DIMS, batch_size: int = 64) -> None:
        from openai import AzureOpenAI

        self.deployment = deployment or config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
        self.dims, self.batch_size = dims, batch_size
        kwargs = dict(azure_endpoint=config.AZURE_OPENAI_ENDPOINT, api_version=config.AZURE_OPENAI_API_VERSION)
        if config.AZURE_OPENAI_API_KEY:
            kwargs["api_key"] = config.AZURE_OPENAI_API_KEY
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        self.client = AzureOpenAI(**kwargs)
        self.total_tokens = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [t.replace("\n", " ") for t in texts[i:i + self.batch_size]]
            resp = self.client.embeddings.create(model=self.deployment, input=batch, dimensions=self.dims)
            self.total_tokens += resp.usage.total_tokens
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out


def get_embedder(prefer_azure: bool | None = None) -> Embedder:
    use_azure = config.AZURE_OPENAI_ENDPOINT != "" if prefer_azure is None else prefer_azure
    return AzureOpenAIEmbedder() if use_azure else HashEmbedder()
