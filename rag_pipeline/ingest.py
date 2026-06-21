"""
rag_pipeline/ingest.py
----------------------
Dual ingest:
  1. PostgreSQL  — raw markdown text from VLLM (via pg_store)
  2. LangChain Chroma — embeddings for semantic retrieval

Usage:
    python -m rag_pipeline.ingest
    python -m rag_pipeline.ingest --data-dir vision_pipeline/data --reset
    python -m rag_pipeline.ingest --skip-pg   # skip postgres, chroma only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import chromadb
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

_HERE      = Path(__file__).resolve().parent
CHROMA_DIR = str(_HERE / "chroma_db")
COLLECTION = "catalog_products"
EMBED_MODEL = "all-MiniLM-L6-v2"


class _ONNXEmbeddings(Embeddings):
    """Thin LangChain wrapper around ChromaDB's built-in ONNX embedder."""
    def __init__(self):
        self._ef = ONNXMiniLM_L6_V2()
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(v) for v in row] for row in self._ef(texts)]
    def embed_query(self, text: str) -> list[float]:
        return [float(v) for v in self._ef([text])[0]]


# ── Metadata helpers (unchanged logic) ───────────────────────────────────────

def _str(val: Any) -> str:
    if val is None or isinstance(val, (list, dict)):
        return ""
    return str(val).strip()


def _flatten_specs(specs: Any) -> str:
    if not specs:
        return ""
    if isinstance(specs, dict):
        return "; ".join(f"{k}: {v}" for k, v in specs.items() if v)
    if isinstance(specs, list):
        return "; ".join(str(s) for s in specs if s)
    return str(specs)


def _extract_metadata(product: dict[str, Any]) -> dict:
    page_meta  = product.get("page_metadata") or {}
    specs      = product.get("specifications") or {}
    children   = product.get("children") or []
    child_codes = ", ".join(c.get("product_code", "") for c in children if c.get("product_code"))

    cat_nos: list[str] = []
    for child in children:
        for row in (child.get("ordering_table") or []):
            if isinstance(row, dict):
                for k, v in row.items():
                    if v and re.search(r"cat|no|ord", k, re.I):
                        cat_nos.append(str(v))

    return {
        "product_name":    _str(product.get("product_name")),
        "product_code":    _str(product.get("product_code")),
        "category":        _str(product.get("category")),
        "sub_category":    _str(product.get("sub_category")),
        "brand":           _str(page_meta.get("brand")),
        "catalog_section": _str(page_meta.get("catalog_section")),
        "page_num":        int(product.get("page_num") or 0),
        "source_pdf":      _str(product.get("source_pdf")),
        "chunk_path":      _str(product.get("chunk_path")),
        "specifications":  _flatten_specs(specs),
        "variant_codes":   child_codes,
        "ordering_nos":    ", ".join(dict.fromkeys(cat_nos)),
        "num_variants":    len(children),
    }


def _load_md_text(chunk_path: str, base_dir: Path) -> str:
    p = Path(chunk_path)
    if not p.is_absolute():
        for c in [p, base_dir.parent.parent / p]:
            if c.exists():
                return c.read_text(encoding="utf-8")
    elif p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _load_all_chunks(data_dir: str) -> list[tuple[str, dict[str, Any]]]:
    results: list[tuple[str, dict[str, Any]]] = []
    base = Path(data_dir)

    for json_path in sorted(base.rglob("products.json")):
        try:
            products = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  WARNING: {json_path}: {e}")
            continue
        if not isinstance(products, list):
            continue

        loaded = 0
        for product in products:
            md_text = _load_md_text(product.get("chunk_path", ""), base)

            if not md_text:
                chunks_dir = json_path.parent / "chunks"
                code = str(product.get("product_code") or "")
                name = str(product.get("product_name") or "")
                slug = re.sub(r"[^a-z0-9]", "_", (code or name).lower())[:20]
                for md_file in sorted(chunks_dir.glob("*.md")):
                    if slug and slug[:8] in md_file.stem:
                        md_text = md_file.read_text(encoding="utf-8")
                        break
                if not md_text:
                    all_mds = sorted(chunks_dir.glob("*.md"))
                    idx = products.index(product)
                    if idx < len(all_mds):
                        md_text = all_mds[idx].read_text(encoding="utf-8")

            if md_text:
                results.append((md_text, product))
                loaded += 1

        print(f"  {json_path}: {loaded}/{len(products)} products paired with .md chunks")

    return results


# ── Main ingest ───────────────────────────────────────────────────────────────

def ingest(
    data_dir: str = str(_HERE.parent / "vision_pipeline" / "data"),
    reset: bool = False,
    skip_pg: bool = False,
) -> int:
    print(f"\nScanning: {data_dir}")
    pairs = _load_all_chunks(data_dir)

    if not pairs:
        print("No chunks found. Run vision_pipeline first.")
        return 0

    print(f"Total product chunks found: {len(pairs)}")

    # ── 1. PostgreSQL: store raw VLLM text ───────────────────────────────────
    if not skip_pg:
        try:
            from rag_pipeline.pg_store import ensure_schema, upsert_chunk
            ensure_schema()
            print("Storing raw text in PostgreSQL...")
            for md_text, product in pairs:
                meta    = _extract_metadata(product)
                content = hashlib.md5(md_text.encode()).hexdigest()[:10]
                code    = meta["product_code"] or meta["product_name"] or "prod"
                doc_id  = re.sub(r"[^a-zA-Z0-9_\-]", "-", code)[:40] + f"_{content}"
                upsert_chunk(doc_id, md_text, meta)
            print(f"  PostgreSQL: {len(pairs)} chunks upserted.")
        except Exception as e:
            print(f"  WARNING: PostgreSQL skipped — {e}")

    # ── 2. LangChain Chroma: store embeddings ─────────────────────────────────
    print(f"Loading embedding model: {EMBED_MODEL} (ONNX) ...")
    embeddings = _ONNXEmbeddings()

    if reset:
        import shutil
        if Path(CHROMA_DIR).exists():
            shutil.rmtree(CHROMA_DIR)
            print("Chroma collection reset.")

    vectorstore = Chroma(
        collection_name=COLLECTION,
        embedding_function=embeddings,
        persist_directory=CHROMA_DIR,
    )

    # Build docs + ids
    docs: list[Document] = []
    doc_ids: list[str]   = []
    for md_text, product in pairs:
        meta    = _extract_metadata(product)
        content = hashlib.md5(md_text.encode()).hexdigest()[:10]
        code    = meta["product_code"] or meta["product_name"] or "prod"
        doc_id  = re.sub(r"[^a-zA-Z0-9_\-]", "-", code)[:40] + f"_{content}"
        docs.append(Document(page_content=md_text, metadata=meta))
        doc_ids.append(doc_id)

    # Skip already-indexed docs
    existing: set[str] = set()
    try:
        existing = set(vectorstore.get(ids=doc_ids)["ids"])
    except Exception:
        pass

    new_docs = [(d, i) for d, i in zip(docs, doc_ids) if i not in existing]

    if not new_docs:
        print(f"All {len(docs)} chunks already in Chroma — nothing to do.")
        return 0

    print(f"Embedding {len(new_docs)} new chunks...")
    batch_docs, batch_ids = zip(*new_docs)
    vectorstore.add_documents(list(batch_docs), ids=list(batch_ids))

    print(f"Added {len(new_docs)} chunks to Chroma at '{CHROMA_DIR}'")
    return len(new_docs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="vision_pipeline/data")
    parser.add_argument("--reset",   action="store_true")
    parser.add_argument("--skip-pg", action="store_true", help="Skip PostgreSQL storage")
    args = parser.parse_args()
    ingest(args.data_dir, args.reset, args.skip_pg)
