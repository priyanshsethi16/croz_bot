"""
rag_pipeline/pg_store.py
------------------------
PostgreSQL store for raw VLLM-extracted chunk text.
Stores/retrieves markdown_text and metadata per product.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_products (
    id            SERIAL PRIMARY KEY,
    doc_id        TEXT UNIQUE NOT NULL,
    product_name  TEXT,
    product_code  TEXT,
    category      TEXT,
    source_pdf    TEXT,
    page_num      INTEGER,
    markdown_text TEXT,
    metadata      JSONB DEFAULT '{}',
    ingested_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_doc_id   ON rag_products (doc_id);
CREATE INDEX IF NOT EXISTS idx_rag_pdf      ON rag_products (source_pdf);
CREATE INDEX IF NOT EXISTS idx_rag_meta     ON rag_products USING gin (metadata);
"""


def get_conn():
    url = os.getenv("POSTGRES_URL")
    if url:
        return psycopg2.connect(url)
    # Fallback: keyword args (safe with special chars in password)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "croz_bot"),
        user=os.getenv("DB_USER", "croz_user"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def ensure_schema():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    conn.commit()
    conn.close()


def upsert_chunk(doc_id: str, md_text: str, meta: dict[str, Any]):
    conn = get_conn()
    sql = """
        INSERT INTO rag_products
            (doc_id, product_name, product_code, category, source_pdf,
             page_num, markdown_text, metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (doc_id) DO UPDATE SET
            markdown_text = EXCLUDED.markdown_text,
            metadata      = EXCLUDED.metadata,
            ingested_at   = NOW()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            doc_id,
            meta.get("product_name", ""),
            meta.get("product_code", ""),
            meta.get("category", ""),
            meta.get("source_pdf", ""),
            meta.get("page_num") or 0,
            md_text,
            json.dumps(meta),
        ))
    conn.commit()
    conn.close()


def get_chunk_by_id(doc_id: str) -> dict | None:
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM rag_products WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def list_chunks(source_pdf: str | None = None) -> list[dict]:
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if source_pdf:
            cur.execute("SELECT * FROM rag_products WHERE source_pdf = %s ORDER BY id", (source_pdf,))
        else:
            cur.execute("SELECT * FROM rag_products ORDER BY id")
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_by_pdf(source_pdf: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rag_products WHERE source_pdf = %s", (source_pdf,))
    conn.commit()
    conn.close()
