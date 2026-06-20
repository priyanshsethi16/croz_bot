"""LangChain provider factories for catalog embeddings and vector storage."""

from __future__ import annotations

import os
from pathlib import Path

from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings


_HERE = Path(__file__).resolve().parent
CHROMA_DIR = str(_HERE / "chroma_db")
COLLECTION = "catalog_products_openai_v1"
EMBED_MODEL = "text-embedding-3-small"


def get_openai_api_key(api_key: str | None = None) -> str:
    key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not key:
        raise ValueError("OPENAI_API_KEY is required for text-embedding-3-small.")
    return key


def build_embeddings(api_key: str | None = None) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=EMBED_MODEL,
        api_key=get_openai_api_key(api_key),
        chunk_size=100,
        max_retries=3,
    )


def _cosine_relevance(distance: float) -> float:
    return max(0.0, min(1.0, 1.0 - float(distance)))


def build_vector_store(api_key: str | None = None, persist_directory: str = CHROMA_DIR) -> Chroma:
    return Chroma(
        collection_name=COLLECTION,
        embedding_function=build_embeddings(api_key),
        persist_directory=persist_directory,
        collection_metadata={"hnsw:space": "cosine", "embedding_model": EMBED_MODEL},
        relevance_score_fn=_cosine_relevance,
    )
