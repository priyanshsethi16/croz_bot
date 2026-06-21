"""
rag_pipeline/retriever.py
--------------------------
LangChain hybrid retriever: BM25 (keyword) + Chroma (semantic).
Optional cross-encoder reranking.
"""
from __future__ import annotations

from typing import Any

import chromadb
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
from langchain_chroma import Chroma
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from rag_pipeline.ingest import CHROMA_DIR, COLLECTION, _ONNXEmbeddings


class HybridRetriever:
    """
    BM25 + semantic ensemble retriever with optional reranking.
    Falls back to semantic-only if BM25 corpus is empty.
    """

    def __init__(self, top_k: int = 5, bm25_weight: float = 0.4):
        self.top_k = top_k
        embeddings  = _ONNXEmbeddings()
        self._vectorstore = Chroma(
            collection_name=COLLECTION,
            embedding_function=embeddings,
            persist_directory=CHROMA_DIR,
        )
        self._bm25_weight      = bm25_weight
        self._semantic_weight  = 1.0 - bm25_weight
        self._retriever        = self._build_retriever()

    def _build_retriever(self):
        semantic = self._vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": self.top_k},
        )

        # Load all docs for BM25 corpus
        try:
            all_data = self._vectorstore.get(include=["documents", "metadatas"])
            docs     = [
                Document(page_content=text, metadata=meta)
                for text, meta in zip(all_data["documents"], all_data["metadatas"])
            ]
        except Exception:
            docs = []

        if not docs:
            return semantic

        bm25 = BM25Retriever.from_documents(docs, k=self.top_k)
        return EnsembleRetriever(
            retrievers=[bm25, semantic],
            weights=[self._bm25_weight, self._semantic_weight],
        )

    def _rerank(self, docs: list[Document], query: str) -> list[Document]:
        """
        Simple TF-IDF-style rerank: score each doc by query term overlap.
        Replace with a cross-encoder model if available.
        """
        query_terms = set(query.lower().split())

        def _score(doc: Document) -> float:
            text  = doc.page_content.lower()
            words = text.split()
            if not words:
                return 0.0
            hits = sum(1 for w in words if w in query_terms)
            return hits / len(words)

        return sorted(docs, key=_score, reverse=True)

    def retrieve(self, query: str) -> list[dict[str, Any]]:
        """
        Returns list of {text, metadata, score} dicts (same contract as old Retriever).
        """
        query = query.strip()
        if not query:
            return []

        if self._vectorstore._collection.count() == 0:
            return []

        raw_docs = self._retriever.invoke(query)
        reranked  = self._rerank(raw_docs[: self.top_k * 2], query)[: self.top_k]

        results = []
        for i, doc in enumerate(reranked):
            results.append({
                "text":     doc.page_content,
                "metadata": doc.metadata,
                "score":    round(1 - i / max(len(reranked), 1), 4),
            })
        return results


# Keep old name as alias so existing imports don't break
Retriever = HybridRetriever
