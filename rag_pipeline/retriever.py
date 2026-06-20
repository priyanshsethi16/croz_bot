"""
rag_pipeline/retriever.py
-------------------------
Embeds a user query and retrieves top-k similar product chunks from ChromaDB.
Returns full .md text as stored — no field assumptions.
"""
from __future__ import annotations

from typing import Any

from rag_pipeline.providers import build_vector_store


class Retriever:
    def __init__(self, top_k: int = 5, api_key: str | None = None):
        self.top_k = top_k
        self._vector_store = build_vector_store(api_key)

    def retrieve(self, query: str) -> list[dict[str, Any]]:
        """
        Embed query, search ChromaDB, return top-k results.
        Each result: {"text": <full md chunk>, "metadata": {...}, "score": float}
        """
        query = query.strip()
        if not query:
            return []

        n = min(self.top_k, self._vector_store._collection.count())
        if n == 0:
            return []

        results = self._vector_store.similarity_search_with_relevance_scores(query, k=n)

        chunks = []
        for document, score in results:
            chunks.append({
                "text": document.page_content,
                "metadata": document.metadata,
                "score": round(float(score), 4),
            })

        return chunks
