"""
db_pipeline/search.py
---------------------
Query interface for the catalog database.

  Exact search  → PostgreSQL (product code, category, PDF)
  Hybrid search → Qdrant dense semantic + sparse BM25 retrieval

Usage:
    python -m db_pipeline.search --exact BPID
    python -m db_pipeline.search --semantic "hammer for demolition work"
    python -m db_pipeline.search --semantic "grease pump" --category "Grease Pumps"
    python -m db_pipeline.search --semantic "drill bits" --pdf Workshop_mini
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


# ── PostgreSQL helpers ────────────────────────────────────────────────────────

def get_pg_conn():
    import psycopg2
    url = os.getenv("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/catalog_db")
    return psycopg2.connect(url)


def exact_search(
    product_code: str = None,
    category: str = None,
    source_pdf: str = None,
    limit: int = 20,
) -> list[dict]:
    """Search PostgreSQL by exact field values."""
    conn = get_pg_conn()
    conditions = []
    params = []

    if product_code:
        conditions.append("UPPER(product_code) = UPPER(%s)")
        params.append(product_code)
    if category:
        conditions.append("LOWER(category) LIKE LOWER(%s)")
        params.append(f"%{category}%")
    if source_pdf:
        conditions.append("source_pdf ILIKE %s")
        params.append(f"%{source_pdf}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"""
        SELECT id, product_code, product_name, category, source_pdf,
               page_num, chunk_file, markdown_text
        FROM products {where}
        ORDER BY product_name
        LIMIT %s
    """
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    conn.close()
    return rows


def fetch_by_ids(pg_ids: list[int]) -> list[dict]:
    """Fetch full product rows from PostgreSQL by id list."""
    if not pg_ids:
        return []
    conn = get_pg_conn()
    sql = """
        SELECT id, product_code, product_name, category, source_pdf,
               page_num, chunk_file, markdown_text
        FROM products
        WHERE id = ANY(%s)
        ORDER BY product_name
    """
    with conn.cursor() as cur:
        cur.execute(sql, (pg_ids,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    return rows


# ── Qdrant hybrid search ─────────────────────────────────────────────────────

def hybrid_search(
    query: str,
    top_k: int = 5,
    category_filter: str = None,
    source_pdf_filter: str = None,
) -> list[dict]:
    """Hybrid dense+BM25 search via Qdrant with optional payload filters."""
    from qdrant_client import models
    from rag_pipeline.retriever import HybridRetriever

    conditions = [
        models.FieldCondition(
            key="metadata.document_type",
            match=models.MatchValue(value="product"),
        )
    ]
    if category_filter:
        conditions.append(models.FieldCondition(
            key="metadata.category",
            match=models.MatchText(text=category_filter),
        ))
    if source_pdf_filter:
        conditions.append(models.FieldCondition(
            key="metadata.source_pdf",
            match=models.MatchValue(value=source_pdf_filter),
        ))

    hits = HybridRetriever(top_k=top_k).retrieve(
        query,
        query_filter=models.Filter(must=conditions),
    )
    return [
        {
            "product_code": hit["metadata"].get("product_code", ""),
            "product_name": hit["metadata"].get("product_name", ""),
            "category": hit["metadata"].get("category", ""),
            "source_pdf": hit["metadata"].get("source_pdf", ""),
            "page_num": hit["metadata"].get("page_num", 0),
            "chunk_file": hit["metadata"].get("chunk_file", ""),
            "markdown_text": hit["text"],
            "similarity": hit["score"],
        }
        for hit in hits
    ]


semantic_search = hybrid_search


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_results(results: list[dict], show_markdown: bool = False):
    if not results:
        print("No results found.")
        return
    for r in results:
        sim = f"  similarity={r.get('similarity', '')}" if "similarity" in r else ""
        print(f"\n{'─'*60}")
        print(f"[{r.get('product_code','?'):12s}] {r.get('product_name','')}")
        print(f"  Category : {r.get('category','')}")
        print(f"  PDF      : {r.get('source_pdf','')}  page {r.get('page_num','')}")
        print(f"  Chunk    : {r.get('chunk_file','')}{sim}")
        if show_markdown and r.get("markdown_text"):
            print(f"\n{r['markdown_text'][:500]}...")


def parse_args():
    parser = argparse.ArgumentParser(description="Search catalog database")
    parser.add_argument("--exact", metavar="CODE", help="Exact product code lookup")
    parser.add_argument("--semantic", metavar="QUERY", help="Natural language semantic search")
    parser.add_argument("--category", help="Filter by category")
    parser.add_argument("--pdf", help="Filter by source PDF name")
    parser.add_argument("--top", type=int, default=5, help="Number of results (default 5)")
    parser.add_argument("--markdown", action="store_true", help="Show markdown content")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.exact:
        results = exact_search(
            product_code=args.exact,
            category=args.category,
            source_pdf=args.pdf,
            limit=args.top,
        )
        print(f"Exact search: '{args.exact}' → {len(results)} result(s)")
        _print_results(results, show_markdown=args.markdown)

    elif args.semantic:
        results = hybrid_search(
            query=args.semantic,
            top_k=args.top,
            category_filter=args.category,
            source_pdf_filter=args.pdf,
        )
        print(f"Hybrid search: '{args.semantic}' → {len(results)} result(s)")
        _print_results(results, show_markdown=args.markdown)

    else:
        print("Specify --exact <code> or --semantic <query>")
        sys.exit(1)


if __name__ == "__main__":
    main()
