"""
Vision Pipeline — Main Entry Point
====================================
Usage:
    python -m vision_pipeline.main --pdf path/to/catalog.pdf
    python -m vision_pipeline.main --pdf catalog.pdf --no-resume
    python -m vision_pipeline.main --pdf catalog.pdf --pages 5 10
    python -m vision_pipeline.main --pdf catalog.pdf --skip-raster
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv(Path(__file__).parent.parent / ".env")

from vision_pipeline.pdf_to_images import rasterize_pdf
from vision_pipeline.key_rotator import KeyRotator
from vision_pipeline.chunk_writer import save_chunk


def _pdf_slug(pdf_path: str) -> str:
    """Derive a clean folder name from the PDF filename."""
    name = Path(pdf_path).stem
    name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    return name[:60].strip("_") or "catalog"


def load_config(config_path: str = None) -> dict:
    default = Path(__file__).parent / "config.yaml"
    path = Path(config_path) if config_path else default
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_checkpoint(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed_pages": [], "product_count": 0}


def save_checkpoint(data: dict, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vision Pipeline — catalog extraction")
    parser.add_argument("--pdf", required=True, help="Path to input PDF catalog")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing checkpoint")
    parser.add_argument("--skip-raster", action="store_true", help="Reuse existing PNG pages")
    parser.add_argument(
        "--pages", nargs=2, type=int, metavar=("START", "END"),
        help="Only process page range e.g. --pages 5 10"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)

    pdf_path = args.pdf
    if not Path(pdf_path).exists():
        print(f"ERROR: PDF not found: {pdf_path}")
        sys.exit(1)

    # ── Per-PDF output folders derived from PDF filename ──────────────────
    slug = _pdf_slug(pdf_path)
    base = Path("vision_pipeline/data") / slug
    pages_dir     = str(base / "pages")
    chunks_dir    = str(base / "chunks")
    products_json = str(base / "products.json")
    checkpoint_file = str(base / "checkpoint.json")

    dpi = int(cfg.get("pdf", {}).get("dpi", 300))

    print("=" * 60)
    print("Vision Pipeline Starting")
    print(f"PDF    : {pdf_path}")
    print(f"Output : {base}/")
    print("=" * 60)

    # ── Step 1: Rasterize ─────────────────────────────────────────────────
    png_paths = rasterize_pdf(pdf_path=pdf_path, pages_dir=pages_dir, dpi=dpi)

    if args.pages:
        start_p, end_p = args.pages
        png_paths = [p for p in png_paths if start_p <= int(p.stem.split("_")[1]) <= end_p]
        print(f"Page filter: {start_p}–{end_p} ({len(png_paths)} pages)")

    # ── Step 2: Checkpoint ────────────────────────────────────────────────
    checkpoint = load_checkpoint(checkpoint_file) if not args.no_resume else {
        "completed_pages": [], "product_count": 0
    }
    completed_pages: set[int] = set(checkpoint.get("completed_pages", []))
    product_count: int = checkpoint.get("product_count", 0)

    all_products: list[dict] = []
    if not args.no_resume and Path(products_json).exists():
        try:
            all_products = json.loads(Path(products_json).read_text(encoding="utf-8"))
            print(f"Resuming: {len(completed_pages)} pages done, {len(all_products)} products so far")
        except Exception:
            all_products = []

    # ── Step 3: Build extractor with key rotation ─────────────────────────
    rotator = KeyRotator(cfg)
    print(f"Provider: {rotator.describe()}")

    pending = [p for p in png_paths if int(p.stem.split("_")[1]) not in completed_pages]
    print(f"Pages to process: {len(pending)} (skipping {len(completed_pages)} already done)")

    with tqdm(total=len(pending), desc="Pages", unit="page") as pbar:
        for png_path in pending:
            page_num = int(png_path.stem.split("_")[1])
            products = rotator.extract_page(png_path, page_num)

            for product in products:
                product_count += 1
                product["source_pdf"] = pdf_path
                chunk_path = save_chunk(product, product_count, chunks_dir)
                product["chunk_path"] = str(chunk_path)
                all_products.append(product)
                tqdm.write(
                    f"  p{page_num:03d} → [{product.get('product_code','?'):12s}] "
                    f"{str(product.get('product_name','?'))[:40]}"
                )

            completed_pages.add(page_num)
            checkpoint["completed_pages"] = sorted(completed_pages)
            checkpoint["product_count"] = product_count
            save_checkpoint(checkpoint, checkpoint_file)

            Path(products_json).parent.mkdir(parents=True, exist_ok=True)
            Path(products_json).write_text(
                json.dumps(all_products, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )

            pbar.update(1)
            pbar.set_postfix({"products": product_count, "provider": rotator.active_provider()})

    print("=" * 60)
    print("Vision Pipeline Complete!")
    print(f"  Pages processed : {len(completed_pages)}")
    print(f"  Products found  : {product_count}")
    print(f"  Chunks saved    : {chunks_dir}/")
    print(f"  JSON manifest   : {products_json}")
    print("=" * 60)


if __name__ == "__main__":
    main()
