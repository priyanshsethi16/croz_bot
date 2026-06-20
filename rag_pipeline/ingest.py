"""
rag_pipeline/ingest.py
----------------------
Reads every .md chunk file produced by vision_pipeline (one per product family).
Uses the markdown content directly as the embedding text — no field hardcoding.
Metadata is extracted generically from the paired products.json entry.

Works for any PDF layout because the .md file already contains everything
chunk_writer rendered: name, code, category, specs, variants, ordering tables.

Usage:
    python -m rag_pipeline.ingest
    python -m rag_pipeline.ingest --data-dir vision_pipeline/data --reset
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from rag_pipeline.providers import CHROMA_DIR, COLLECTION, EMBED_MODEL, build_vector_store

# Absolute path — works regardless of which directory the process runs from
_HERE = Path(__file__).resolve().parent


# ── Metadata extractor (generic — works for any md layout) ───────────────────

def _extract_metadata(md_text: str, product: dict[str, Any]) -> dict:
    """
    Build a flat metadata dict from the product JSON entry.
    Only stores scalar values (str/int/float/bool) — ChromaDB requirement.
    Falls back gracefully for any missing field so it works across all PDF types.
    """

    def _str(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, (list, dict)):
            return ""
        return str(val).strip()

    def _flatten_specs(specs: Any) -> str:
        """Turn any specs shape (dict or list) into a single string."""
        if not specs:
            return ""
        if isinstance(specs, dict):
            return "; ".join(f"{k}: {v}" for k, v in specs.items() if v)
        if isinstance(specs, list):
            return "; ".join(str(s) for s in specs if s)
        return str(specs)

    page_meta = product.get("page_metadata") or {}
    specs     = product.get("specifications") or {}

    # Collect all variant codes generically
    children   = product.get("children") or []
    child_codes = ", ".join(
        c.get("product_code", "") for c in children if c.get("product_code")
    )

    # Collect all ordering cat_nos generically (any key containing "cat" or "no")
    cat_nos: list[str] = []
    for child in children:
        for row in (child.get("ordering_table") or []):
            if isinstance(row, dict):
                for k, v in row.items():
                    if v and re.search(r"cat|no|ord", k, re.I):
                        cat_nos.append(str(v))
    ordering_nos = ", ".join(dict.fromkeys(cat_nos))   # deduplicated, ordered

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
        "ordering_nos":    ordering_nos,
        "num_variants":    len(children),
    }


# ── Chunk loader ──────────────────────────────────────────────────────────────

def _load_md_text(chunk_path: str, base_dir: Path) -> str:
    """
    Load markdown text from the chunk_path stored in products.json.
    chunk_path may be relative (to project root) or absolute.
    """
    p = Path(chunk_path)
    if not p.is_absolute():
        # Try relative to cwd first, then relative to data base_dir
        candidates = [p, base_dir.parent.parent / p]
        for c in candidates:
            if c.exists():
                return c.read_text(encoding="utf-8")
    elif p.exists():
        return p.read_text(encoding="utf-8")
    return ""


def _load_all_chunks(data_dir: str) -> list[tuple[str, dict[str, Any]]]:
    """
    Walk data_dir, find every products.json, pair each product with its .md file.
    Returns list of (md_text, product_dict).
    """
    results: list[tuple[str, dict[str, Any]]] = []
    base = Path(data_dir)

    for json_path in sorted(base.rglob("products.json")):
        try:
            products = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  WARNING: Cannot read {json_path}: {e}")
            continue

        if not isinstance(products, list):
            continue

        loaded = 0
        for product in products:
            chunk_path = product.get("chunk_path", "")

            # Try loading the .md file
            md_text = ""
            if chunk_path:
                md_text = _load_md_text(chunk_path, base)

            # Fallback: search chunks/ dir next to this products.json
            if not md_text:
                chunks_dir = json_path.parent / "chunks"
                code  = str(product.get("product_code") or "")
                name  = str(product.get("product_name") or "")
                # find any .md whose name contains part of the code or name
                slug  = re.sub(r"[^a-z0-9]", "_", (code or name).lower())[:20]
                for md_file in sorted(chunks_dir.glob("*.md")):
                    if slug and slug[:8] in md_file.stem:
                        md_text = md_file.read_text(encoding="utf-8")
                        break
                # last resort: just grab them in order
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

def ingest(data_dir: str = str(_HERE.parent / "vision_pipeline" / "data"), reset: bool = False) -> int:
    print(f"\nScanning: {data_dir}")
    pairs = _load_all_chunks(data_dir)

    if not pairs:
        print("No chunks found. Run vision_pipeline first to generate .md files.")
        return 0

    print(f"Total product chunks found: {len(pairs)}")
    print(f"Embedding provider: OpenAI / {EMBED_MODEL}")
    vector_store = build_vector_store()
    if reset:
        vector_store.reset_collection()
        print("Existing OpenAI embedding collection reset.")

    collection = vector_store._collection

    # Build full list of (id, md_text, metadata) for all found chunks
    all_ids, all_texts, all_metas = [], [], []
    for md_text, product in pairs:
        content_hash = hashlib.md5(md_text.encode()).hexdigest()[:10]
        code    = str(product.get("product_code") or "").strip()
        name    = str(product.get("product_name") or "product").strip()
        id_base = re.sub(r"[^a-zA-Z0-9_\-]", "-", code if code else name)[:40]
        doc_id  = f"{id_base}_{content_hash}"
        all_ids.append(doc_id)
        all_texts.append(md_text)
        all_metas.append(_extract_metadata(md_text, product))

    # Find which ids are already in ChromaDB — skip those, only embed new ones
    existing_ids: set[str] = set()
    if collection.count() > 0:
        existing_ids = set(collection.get(ids=all_ids, include=[])["ids"])

    new_indices = [i for i, doc_id in enumerate(all_ids) if doc_id not in existing_ids]

    if not new_indices:
        print(f"All {len(all_ids)} chunks already in ChromaDB — nothing to do.")
        print(f"Collection '{COLLECTION}' has {collection.count()} documents.")
        return 0

    print(f"Skipping {len(existing_ids)} already ingested | Embedding {len(new_indices)} new chunks ...")

    new_texts = [all_texts[i] for i in new_indices]
    new_metas = [all_metas[i] for i in new_indices]
    new_ids   = [all_ids[i]   for i in new_indices]

    batch = 100
    for start in range(0, len(new_ids), batch):
        stop = start + batch
        documents = [
            Document(page_content=text, metadata=metadata)
            for text, metadata in zip(new_texts[start:stop], new_metas[start:stop])
        ]
        vector_store.add_documents(
            documents=documents,
            ids=new_ids[start:stop],
        )
        print(f"  Embedded {min(stop, len(new_ids))}/{len(new_ids)} chunks")

    print(f"\nAdded {len(new_ids)} new product chunks into ChromaDB at '{CHROMA_DIR}'")
    print(f"Collection '{COLLECTION}' now has {collection.count()} documents.")
    return len(new_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="vision_pipeline/data")
    parser.add_argument("--reset", action="store_true", help="Delete and recreate collection")
    args = parser.parse_args()
    ingest(args.data_dir, args.reset)
