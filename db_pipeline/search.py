"""
db_pipeline/search.py
---------------------
Query interface for the catalog database.

  Exact search  → PostgreSQL (product code, category, PDF)
  Semantic search → ChromaDB (natural language queries)
  Hybrid search  → Chroma finds candidates, PostgreSQL fetches full data

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


# ── ChromaDB helpers ──────────────────────────────────────────────────────────

def get_chroma_collection(chroma_path: str = "db_pipeline/chroma_db"):
    from rag_pipeline.providers import build_vector_store
    return build_vector_store(persist_directory=chroma_path)


def semantic_search(
    query: str,
    top_k: int = 5,
    category_filter: str = None,
    source_pdf_filter: str = None,
    chroma_path: str = "db_pipeline/chroma_db",
) -> list[dict]:
    """
    Semantic search via ChromaDB embeddings.
    Returns full product data fetched from PostgreSQL.
    """
    vector_store = get_chroma_collection(chroma_path)

    where: dict = {}
    if category_filter:
        where["category"] = {"$contains": category_filter}
    if source_pdf_filter:
        where["source_pdf"] = {"$contains": source_pdf_filter}

    results = vector_store.similarity_search_with_relevance_scores(
        query,
        k=top_k,
        filter=where if where else None,
    )

    if not results:
        return []

    pg_ids = [int(document.metadata["postgres_id"]) for document, _ in results]

    products = fetch_by_ids(pg_ids)

    # Attach similarity score (1 - cosine distance)
    id_to_score = {pg_ids[i]: round(float(results[i][1]), 4) for i in range(len(pg_ids))}
    for p in products:
        p["similarity"] = id_to_score.get(p["id"], 0.0)

    products.sort(key=lambda x: -x["similarity"])
    return products


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
    parser.add_argument("--chroma-path", default="db_pipeline/chroma_db")
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
        results = semantic_search(
            query=args.semantic,
            top_k=args.top,
            category_filter=args.category,
            source_pdf_filter=args.pdf,
            chroma_path=args.chroma_path,
        )
        print(f"Semantic search: '{args.semantic}' → {len(results)} result(s)")
        _print_results(results, show_markdown=args.markdown)

    else:
        print("Specify --exact <code> or --semantic <query>")
        sys.exit(1)


if __name__ == "__main__":
    main()
