"""
db_pipeline/ingest.py
---------------------
Reads all .md chunk files → inserts into:
  1. PostgreSQL (structured raw data)
  2. ChromaDB  (embeddings for semantic search)

Usage:
    # Ingest all chunks from a specific PDF run
    python -m db_pipeline.ingest --chunks vision_pipeline/data/Workshop_mini/chunks

    # Ingest all PDFs
    python -m db_pipeline.ingest --all

    # Re-ingest (wipe existing data for this PDF first)
    python -m db_pipeline.ingest --chunks vision_pipeline/data/Workshop_mini/chunks --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv(Path(__file__).parent.parent / ".env")


# ── Markdown parser ───────────────────────────────────────────────────────────

def parse_chunk(md_path: Path) -> dict:
    """Extract structured fields from a markdown chunk file."""
    text = md_path.read_text(encoding="utf-8")
    data: dict = {
        "product_code": "",
        "product_name": "",
        "category": "",
        "page_num": None,
        "markdown_text": text,
        "chunk_file": str(md_path),
        "metadata": {},
    }

    lines = text.splitlines()

    # Product name — first # heading
    for line in lines:
        if line.startswith("# "):
            data["product_name"] = line[2:].strip()
            break

    # Extract **Field:** `value` or **Field:** value patterns
    field_re = re.compile(r"\*\*(.+?):\*\*\s*`?(.+?)`?\s*$")
    for line in lines:
        m = field_re.match(line.strip())
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if key == "Product Code":
                data["product_code"] = val
            elif key == "Category":
                data["category"] = val
            elif key == "Source Page":
                try:
                    data["page_num"] = int(val)
                except ValueError:
                    pass

    # Everything else goes into metadata as raw text sections
    sections: dict = {}
    current = None
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current and buf:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current:
            buf.append(line)
    if current and buf:
        sections[current] = "\n".join(buf).strip()

    data["metadata"] = sections
    return data


# ── PostgreSQL ────────────────────────────────────────────────────────────────

def get_pg_conn():
    import psycopg2
    url = os.getenv("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/catalog_db")
    return psycopg2.connect(url)


def ensure_schema(conn):
    schema_path = Path(__file__).parent / "schema.sql"
    if schema_path.exists():
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text())
        conn.commit()


def insert_postgres(conn, chunk: dict, source_pdf: str) -> int:
    """Insert one chunk into PostgreSQL. Returns the new row id."""
    import psycopg2.extras
    sql = """
        INSERT INTO products
            (product_code, product_name, category, source_pdf, page_num,
             chunk_file, markdown_text, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            chunk["product_code"],
            chunk["product_name"],
            chunk["category"],
            source_pdf,
            chunk["page_num"],
            chunk["chunk_file"],
            chunk["markdown_text"],
            json.dumps(chunk["metadata"]),
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def delete_by_pdf(conn, source_pdf: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM products WHERE source_pdf = %s", (source_pdf,))
    conn.commit()


# ── ChromaDB ──────────────────────────────────────────────────────────────────

def get_chroma_collection(chroma_path: str = "db_pipeline/chroma_db"):
    from rag_pipeline.providers import build_vector_store
    return build_vector_store(persist_directory=chroma_path)


def insert_chroma(collection, chunk: dict, pg_id: int, source_pdf: str):
    """Upsert one chunk embedding into Chroma."""
    doc_id = f"pg_{pg_id}"
    collection.add_texts(
        texts=[chunk["markdown_text"]],
        ids=[doc_id],
        metadatas=[{
            "postgres_id": pg_id,
            "product_code": chunk["product_code"] or "",
            "product_name": chunk["product_name"] or "",
            "category": chunk["category"] or "",
            "source_pdf": source_pdf,
            "page_num": chunk["page_num"] or 0,
            "chunk_file": chunk["chunk_file"],
        }],
    )


def delete_chroma_by_pdf(collection, source_pdf: str):
    results = collection.get(where={"source_pdf": source_pdf})
    if results["ids"]:
        collection.delete(ids=results["ids"])


# ── Main ingest logic ─────────────────────────────────────────────────────────

def ingest_chunks_dir(
    chunks_dir: Path,
    source_pdf: str,
    force: bool = False,
    chroma_path: str = "db_pipeline/chroma_db",
):
    md_files = sorted(chunks_dir.glob("*.md"))
    if not md_files:
        print(f"No .md files found in {chunks_dir}")
        return

    print(f"Found {len(md_files)} chunks in {chunks_dir}")

    conn = get_pg_conn()
    ensure_schema(conn)
    collection = get_chroma_collection(chroma_path)

    if force:
        print(f"Force mode: deleting existing data for {source_pdf}")
        delete_by_pdf(conn, source_pdf)
        delete_chroma_by_pdf(collection, source_pdf)

    ingested = 0
    with tqdm(total=len(md_files), desc="Ingesting", unit="chunk") as pbar:
        for md_path in md_files:
            chunk = parse_chunk(md_path)
            pg_id = insert_postgres(conn, chunk, source_pdf)
            insert_chroma(collection, chunk, pg_id, source_pdf)
            ingested += 1
            pbar.update(1)
            pbar.set_postfix({"code": chunk["product_code"] or "?"})

    conn.close()
    print(f"Ingested {ingested} chunks → PostgreSQL + ChromaDB")


def parse_args():
    parser = argparse.ArgumentParser(description="Ingest catalog chunks into PostgreSQL + Chroma")
    parser.add_argument("--chunks", help="Path to a specific chunks directory")
    parser.add_argument("--all", action="store_true", help="Ingest all PDF subdirectories")
    parser.add_argument("--force", action="store_true", help="Re-ingest (delete existing first)")
    parser.add_argument(
        "--chroma-path", default="db_pipeline/chroma_db",
        help="Path to ChromaDB storage directory"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    chroma_path = args.chroma_path

    if args.chunks:
        chunks_dir = Path(args.chunks)
        if not chunks_dir.exists():
            print(f"ERROR: {chunks_dir} does not exist")
            sys.exit(1)
        # Derive source_pdf from parent folder name
        source_pdf = chunks_dir.parent.name
        ingest_chunks_dir(chunks_dir, source_pdf, force=args.force, chroma_path=chroma_path)

    elif args.all:
        base = Path("vision_pipeline/data")
        pdf_dirs = [d for d in base.iterdir() if d.is_dir() and (d / "chunks").exists()]
        if not pdf_dirs:
            print("No PDF output directories found under vision_pipeline/data/")
            sys.exit(1)
        for pdf_dir in sorted(pdf_dirs):
            print(f"\n── {pdf_dir.name} ──────────────────────────")
            ingest_chunks_dir(
                pdf_dir / "chunks",
                source_pdf=pdf_dir.name,
                force=args.force,
                chroma_path=chroma_path,
            )
    else:
        print("Specify --chunks <dir> or --all")
        sys.exit(1)


if __name__ == "__main__":
    main()
