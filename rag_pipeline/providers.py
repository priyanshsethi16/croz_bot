"""Qdrant, OpenAI dense, and BM25 sparse providers for hybrid search."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from langchain_openai import OpenAIEmbeddings
from qdrant_client import QdrantClient, models


_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
COLLECTION = os.getenv("QDRANT_COLLECTION", "catalog_chunks_v2")
COLLECTION_ALIAS = os.getenv("QDRANT_ALIAS", "catalog_chunks_current")

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSIONS = 1536
SPARSE_MODEL = "Qdrant/bm25"


def get_openai_api_key(api_key: str | None = None) -> str:
    key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not key:
        raise ValueError(f"OPENAI_API_KEY is required for {EMBED_MODEL}.")
    return key


def build_embeddings(api_key: str | None = None) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=EMBED_MODEL,
        api_key=get_openai_api_key(api_key),
        chunk_size=100,
        max_retries=3,
    )


@lru_cache(maxsize=1)
def build_sparse_embeddings() -> FastEmbedSparse:
    return FastEmbedSparse(model_name=SPARSE_MODEL)


def build_qdrant_client() -> QdrantClient:
    return QdrantClient(
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY,
        timeout=30,
        prefer_grpc=False,
    )


def ensure_collection(*, reset: bool = False) -> QdrantClient:
    """Create the source-aware catalog collection and payload indexes."""
    client = build_qdrant_client()
    exists = client.collection_exists(COLLECTION)
    if reset and exists:
        client.delete_collection(COLLECTION)
        exists = False

    if not exists:
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(
                    size=EMBED_DIMENSIONS,
                    distance=models.Distance.COSINE,
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR: models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=False),
                    modifier=models.Modifier.IDF,
                ),
            },
            on_disk_payload=True,
        )

    payload_indexes = {
        "metadata.catalog_id": models.PayloadSchemaType.KEYWORD,
        "metadata.document_id": models.PayloadSchemaType.KEYWORD,
        "metadata.product_family_id": models.PayloadSchemaType.KEYWORD,
        "metadata.product_variant_id": models.PayloadSchemaType.KEYWORD,
        "metadata.product_code": models.PayloadSchemaType.KEYWORD,
        "metadata.linked_product_codes": models.PayloadSchemaType.KEYWORD,
        "metadata.normalized_category": models.PayloadSchemaType.KEYWORD,
        "metadata.normalized_subcategory": models.PayloadSchemaType.KEYWORD,
        "metadata.source_schema": models.PayloadSchemaType.KEYWORD,
        "metadata.source_type": models.PayloadSchemaType.KEYWORD,
        "metadata.source_pdf": models.PayloadSchemaType.KEYWORD,
        "metadata.is_active": models.PayloadSchemaType.BOOL,
        "metadata.page_start": models.PayloadSchemaType.INTEGER,
        "metadata.page_end": models.PayloadSchemaType.INTEGER,
        "metadata.schema_version": models.PayloadSchemaType.INTEGER,
        "metadata.chunk_hash": models.PayloadSchemaType.KEYWORD,
    }
    for field_name, field_schema in payload_indexes.items():
        client.create_payload_index(
            collection_name=COLLECTION,
            field_name=field_name,
            field_schema=field_schema,
        )
    return client


def build_vector_store(api_key: str | None = None) -> QdrantVectorStore:
    client = build_qdrant_client()
    if not client.collection_exists(COLLECTION):
        raise RuntimeError(
            f"Qdrant collection '{COLLECTION}' does not exist. Initialize catalog indexing first."
        )
    aliases = {alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases}
    query_collection = COLLECTION_ALIAS if aliases.get(COLLECTION_ALIAS) == COLLECTION else COLLECTION
    return QdrantVectorStore(
        client=client,
        collection_name=query_collection,
        embedding=build_embeddings(api_key),
        sparse_embedding=build_sparse_embeddings(),
        retrieval_mode=RetrievalMode.HYBRID,
        vector_name=DENSE_VECTOR,
        sparse_vector_name=SPARSE_VECTOR,
        distance=models.Distance.COSINE,
    )


def activate_collection_alias() -> None:
    """Atomically point the stable query alias at the catalog collection."""
    client = build_qdrant_client()
    if not client.collection_exists(COLLECTION):
        raise RuntimeError(f"Collection '{COLLECTION}' does not exist.")
    aliases = {alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases}
    operations = []
    if COLLECTION_ALIAS in aliases:
        operations.append(models.DeleteAliasOperation(
            delete_alias=models.DeleteAlias(alias_name=COLLECTION_ALIAS),
        ))
    operations.append(models.CreateAliasOperation(
        create_alias=models.CreateAlias(collection_name=COLLECTION, alias_name=COLLECTION_ALIAS),
    ))
    client.update_collection_aliases(change_aliases_operations=operations)


def indexed_document_count() -> int:
    client = build_qdrant_client()
    if not client.collection_exists(COLLECTION):
        return 0
    return client.count(collection_name=COLLECTION, exact=True).count
