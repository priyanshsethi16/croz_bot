"""
rag_pipeline/retriever.py
-------------------------
Embeds a user query and retrieves top-k similar product chunks from ChromaDB.
Returns full .md text as stored — no field assumptions.
"""
from __future__ import annotations

from typing import Any

import chromadb
from sentence_transformers import SentenceTransformer

from rag_pipeline.ingest import CHROMA_DIR, COLLECTION, EMBED_MODEL


class Retriever:
    def __init__(self, top_k: int = 5):
        self.top_k = top_k
        self._model = SentenceTransformer(EMBED_MODEL)
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        self._collection = client.get_collection(COLLECTION)

    def retrieve(self, query: str) -> list[dict[str, Any]]:
        """
        Embed query, search ChromaDB, return top-k results.
        Each result: {"text": <full md chunk>, "metadata": {...}, "score": float}
        """
        query = query.strip()
        if not query:
            return []

        vec = self._model.encode([query])[0].tolist()
        n   = min(self.top_k, self._collection.count())
        if n == 0:
            return []

        results = self._collection.query(
            query_embeddings=[vec],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )

        chunks = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            chunks.append({
                "text":     doc,
                "metadata": meta,
                "score":    round(1 - dist, 4),
            })

        return chunks
