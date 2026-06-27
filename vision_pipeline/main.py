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
from vision_pipeline.assembler import assemble_product_families
from vision_pipeline.schema import validate_page_products


def _pdf_slug(pdf_path: str) -> str:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vision Pipeline — catalog extraction")
    parser.add_argument("--pdf", required=True, help="Path to input PDF catalog")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing checkpoint")
    parser.add_argument("--skip-raster", action="store_true", help="Reuse existing PNG pages")
    parser.add_argument("--document-id", default="", help="Trusted CatalogDocument UUID injected by V2 ingestion")
    parser.add_argument("--source-pdf", default="", help="Original PDF filename used for citations")
    parser.add_argument("--output-slug", default="", help="Stable artifact folder name")
    parser.add_argument(
        "--parsing-instructions",
        default="",
        help="Optional PDF-specific admin instructions for VLM extraction",
    )
    parser.add_argument(
        "--page-offset",
        type=int,
        default=0,
        help="Add this offset to split-PDF page numbers to preserve original pages",
    )
    parser.add_argument(
        "--pages", nargs=2, type=int, metavar=("START", "END"),
        help="Only process page range e.g. --pages 5 10"
    )
    return parser.parse_args()


def _ingest_to_db(pdf_path: Path, assembled: list[dict]) -> None:
    """Write assembled products directly into Postgres via Django ORM."""
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    groz_ui_dir = str(PROJECT_ROOT / "groz_ui")
    if groz_ui_dir not in sys.path:
        sys.path.insert(0, groz_ui_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "groz_ui.settings")

    import django
    from django.conf import settings as _settings
    if not _settings.configured:
        django.setup()

    from catalog.views import _ensure_catalog_document, _ingest_assembled_products_for_document
    doc = _ensure_catalog_document(pdf_path)
    _ingest_assembled_products_for_document(doc, assembled)
    print(f"  → Ingested {len(assembled)} product(s) into Postgres for '{pdf_path.name}'")


def main():
    args = parse_args()
    cfg = load_config(args.config)
    parsing_instructions = (
        args.parsing_instructions.strip()
        or os.getenv("PDF_PARSING_INSTRUCTIONS", "").strip()
    )
    if parsing_instructions:
        cfg["parsing_instructions"] = parsing_instructions[:4000]
        cfg.setdefault("gemini", {})["parsing_instructions"] = cfg["parsing_instructions"]

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: PDF not found: {args.pdf}")
        sys.exit(1)

    # Pages go to a temp folder (rasterize needs a dir), cleaned up after
    slug = _pdf_slug(args.output_slug) if args.output_slug else _pdf_slug(str(pdf_path))
    pages_dir = str(Path("vision_pipeline/data") / slug / "pages")

    dpi = int(cfg.get("pdf", {}).get("dpi", 300))

    print("=" * 60)
    print("Vision Pipeline Starting")
    print(f"PDF    : {pdf_path}")
    print("=" * 60)

    # ── Step 1: Rasterize ─────────────────────────────────────────────────
    png_paths = rasterize_pdf(pdf_path=str(pdf_path), pages_dir=pages_dir, dpi=dpi)

    if args.pages:
        start_p, end_p = args.pages
        png_paths = [p for p in png_paths if start_p <= int(p.stem.split("_")[1]) <= end_p]
        print(f"Page filter: {start_p}–{end_p} ({len(png_paths)} pages)")

    # ── Step 2: Build extractor ───────────────────────────────────────────
    rotator = KeyRotator(cfg)
    print(f"Provider: {rotator.describe()}")
    print(f"Pages to process: {len(png_paths)}")

    all_products: list[dict] = []
    failed_pages: list[int] = []

    with tqdm(total=len(png_paths), desc="Pages", unit="page") as pbar:
        for png_path in png_paths:
            page_num = int(png_path.stem.split("_")[1])
            products = rotator.extract_page(png_path, page_num)

            if products is None:
                tqdm.write(f"  p{page_num:03d} → [FAILED] all retries exhausted")
                failed_pages.append(page_num)
                pbar.update(1)
                continue

            if not products:
                tqdm.write(f"  p{page_num:03d} → [EMPTY] no products found")

            validated, validation_issues = validate_page_products(
                products,
                page_num=page_num,
                page_offset=args.page_offset,
                source_pdf=args.source_pdf or pdf_path.name,
                document_id=args.document_id,
            )
            if validation_issues:
                tqdm.write(f"  p{page_num:03d} → [VALIDATION] {len(validation_issues)} invalid entr{'y' if len(validation_issues) == 1 else 'ies'}")
            if products and not validated:
                tqdm.write(f"  p{page_num:03d} → [FAILED] no valid entries")
                failed_pages.append(page_num)
                pbar.update(1)
                continue

            for vp in validated:
                product = vp.model_dump(mode='json')
                all_products.append(product)
                tqdm.write(
                    f"  p{page_num:03d} → [{str(product.get('product_code') or '?'):12s}] "
                    f"{str(product.get('product_name') or '?')[:40]}"
                )

            pbar.update(1)
            pbar.set_postfix({"products": len(all_products), "provider": rotator.active_provider(), "failed": len(failed_pages)})

    if failed_pages:
        print(f"\nWARNING: {len(failed_pages)} pages failed: {failed_pages}")

    # ── Step 3: Assemble + ingest directly into Postgres ──────────────────
    assembled = assemble_product_families(all_products)

    print("=" * 60)
    print("Vision Pipeline Complete!")
    print(f"  Pages processed : {len(png_paths) - len(failed_pages)}")
    print(f"  Products found  : {len(all_products)}")
    print(f"  Families        : {len(assembled)}")
    print("=" * 60)

    if assembled:
        try:
            _ingest_to_db(pdf_path, assembled)
        except Exception as e:
            print(f"ERROR: Postgres ingestion failed: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)
    else:
        print("No products extracted — nothing written to Postgres.")

    # ── Step 4: Cleanup entire artifact directory to free disk space ──────
    try:
        import shutil
        artifact_dir = Path(pages_dir).parent
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
            print(f"  → Cleaned up artifact dir: {artifact_dir}")
    except Exception as e:
        print(f"WARNING: Could not clean up artifact dir: {e}")

    if failed_pages:
        sys.exit(2)


if __name__ == "__main__":
    main()
