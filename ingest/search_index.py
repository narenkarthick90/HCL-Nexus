"""Azure AI Search index definition, synonym maps and upload (free tier friendly)."""
from __future__ import annotations

import json
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration, HnswParameters, ScoringProfile, SearchField, SearchFieldDataType,
    SearchIndex, SearchableField, SemanticConfiguration, SemanticField, SemanticPrioritizedFields,
    SemanticSearch, SimpleField, SynonymMap, TextWeights, VectorSearch, VectorSearchProfile,
)

from . import config
from .models import Chunk

ANALYZER = "en.microsoft"
VECTOR_PROFILE = "hnsw-profile"
SEMANTIC_CONFIG = "sem-default"     # semantic ranker is free-tier in Korea Central; dropped automatically elsewhere


def credential():
    if config.AZURE_SEARCH_API_KEY:
        return AzureKeyCredential(config.AZURE_SEARCH_API_KEY)
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def index_client() -> SearchIndexClient:
    return SearchIndexClient(config.AZURE_SEARCH_ENDPOINT, credential())


def search_client(index_name: str) -> SearchClient:
    return SearchClient(config.AZURE_SEARCH_ENDPOINT, index_name, credential())


def index_definition(name: str, synonym_map: str, dims: int = config.EMBEDDING_DIMS,
                     semantic: bool = True) -> SearchIndex:
    syn = [synonym_map]
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="parent_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SearchableField(name="title", analyzer_name=ANALYZER, filterable=True, synonym_map_names=syn),
        SearchableField(name="section_path", analyzer_name=ANALYZER, synonym_map_names=syn),
        SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="domain", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="doc_version", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="effective_date", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="owner", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="source_url", type=SearchFieldDataType.String),
        SimpleField(name="file_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="text", analyzer_name=ANALYZER),
        SearchableField(name="content", analyzer_name=ANALYZER, synonym_map_names=syn),
        SimpleField(name="parent_text", type=SearchFieldDataType.String),
        SimpleField(name="initiative_id", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="section", analyzer_name=ANALYZER, filterable=True, facetable=True),
        SimpleField(name="source_file", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="injection_flag", type=SearchFieldDataType.Boolean, filterable=True),
        SearchField(name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, stored=False, vector_search_dimensions=dims,
                    vector_search_profile_name=VECTOR_PROFILE),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(
            name="hnsw", parameters=HnswParameters(m=4, ef_construction=400, ef_search=500, metric="cosine"))],
        profiles=[VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name="hnsw")],
    )
    headings_boost = ScoringProfile(
        name="boost-headings",
        text_weights=TextWeights(weights={"title": 2.0, "section_path": 3.0, "content": 1.0, "text": 1.0}))
    semantic_search = SemanticSearch(configurations=[SemanticConfiguration(
        name=SEMANTIC_CONFIG,
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[SemanticField(field_name="text")],
            keywords_fields=[SemanticField(field_name="section_path")]))]) if semantic else None
    return SearchIndex(name=name, fields=fields, vector_search=vector_search, semantic_search=semantic_search,
                       scoring_profiles=[headings_boost], default_scoring_profile="boost-headings")


def load_synonyms(domain: str, path: Path = Path(__file__).with_name("synonyms.json")) -> list[str]:
    return json.loads(path.read_text("utf-8"))[domain]


def ensure_synonym_map(client: SearchIndexClient, domain: str) -> str:
    name = config.SYNONYM_MAPS[domain]
    client.create_or_update_synonym_map(SynonymMap(name=name, synonyms="\n".join(load_synonyms(domain))))
    return name


def ensure_index(client: SearchIndexClient, domain: str, recreate: bool = False) -> str:
    name = config.INDEXES[domain]
    if recreate:
        try:
            client.delete_index(name)
        except Exception:
            pass
    try:
        client.create_or_update_index(index_definition(name, config.SYNONYM_MAPS[domain], semantic=True))
    except HttpResponseError as e:
        if "semantic" not in str(e).lower():
            raise
        print(f"  semantic ranker not available on this service ({e.message.splitlines()[0]}); "
              f"creating {name} without it")
        client.create_or_update_index(index_definition(name, config.SYNONYM_MAPS[domain], semantic=False))
    return name


def upload_chunks(index_name: str, chunks: list[Chunk], batch_size: int = 100) -> int:
    client = search_client(index_name)
    ok = 0
    for i in range(0, len(chunks), batch_size):
        results = client.upload_documents([c.to_search_doc() for c in chunks[i:i + batch_size]])
        failed = [r for r in results if not r.succeeded]
        ok += len(results) - len(failed)
        for r in failed:
            print(f"  FAILED {r.key}: {r.error_message}")
    return ok
