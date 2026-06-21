"""Native Qdrant hybrid retrieval for product and manual chunks."""

from __future__ import annotations

from typing import Any

from qdrant_client import models

from rag_pipeline.providers import build_vector_store


class HybridRetriever:
    """Fuse dense semantic and sparse BM25 rankings inside Qdrant using RRF."""

    def __init__(
        self,
        top_k: int = 5,
        api_key: str | None = None,
        *,
        candidate_multiplier: int = 4,
        semantic_weight: float = 0.4,
        keyword_weight: float = 0.6,
        rrf_k: int = 2,
        vector_store=None,
    ):
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero.")
        if candidate_multiplier <= 0:
            raise ValueError("candidate_multiplier must be greater than zero.")
        if semantic_weight < 0 or keyword_weight < 0 or semantic_weight + keyword_weight <= 0:
            raise ValueError("Retrieval weights must be non-negative and not both zero.")
        if rrf_k < 0:
            raise ValueError("rrf_k must be non-negative.")

        self.top_k = top_k
        self.candidate_multiplier = candidate_multiplier
        total_weight = semantic_weight + keyword_weight
        self.semantic_weight = semantic_weight / total_weight
        self.keyword_weight = keyword_weight / total_weight
        self.rrf_k = rrf_k
        self._vector_store = vector_store or build_vector_store(api_key)

    def retrieve(
        self,
        query: str,
        *,
        query_filter: models.Filter | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k results from Qdrant's dense+sparse RRF query."""
        query = query.strip()
        if not query:
            return []

        count = self._vector_store.client.count(
            collection_name=self._vector_store.collection_name,
            exact=True,
        ).count
        if count == 0:
            return []

        candidate_limit = min(
            count,
            max(self.top_k, self.top_k * self.candidate_multiplier),
        )
        results = self._vector_store.similarity_search_with_score(
            query,
            k=candidate_limit,
            filter=query_filter,
            hybrid_fusion=models.RrfQuery(
                rrf=models.Rrf(
                    k=self.rrf_k,
                    weights=[self.semantic_weight, self.keyword_weight],
                )
            ),
        )

        return [
            {
                "text": document.page_content,
                "metadata": document.metadata,
                "score": round(float(score), 4),
            }
            for document, score in results[: self.top_k]
        ]


Retriever = HybridRetriever
