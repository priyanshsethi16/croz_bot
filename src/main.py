"""
Catalog Parser — Main Entry Point

Usage:
    python -m src.main --pdf path/to/catalog.pdf
    python -m src.main --pdf catalog.pdf --no-resume
    python -m src.main --pdf catalog.pdf --skip-ocr   # Use existing OCR cache only
"""
import argparse
import json
import sys
from pathlib import Path

from tqdm import tqdm

from src.config import load_config
from src.models.product import CheckpointData
from src.ocr.mistral_ocr import MistralOCR
from src.extraction.boundary_detector import detect_all_boundaries
from src.extraction.product_parser import ProductParser
from src.extraction.chunk_generator import generate_and_save_chunk
from src.utils.file_utils import (
    save_checkpoint,
    load_checkpoint,
    clear_checkpoint,
    save_products_json,
    list_ocr_pages,
)
from src.utils.logger import logger, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Industrial PDF Catalog Parser")
    parser.add_argument("--pdf", required=True, help="Path to input PDF catalog")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing checkpoint")
    parser.add_argument("--skip-ocr", action="store_true", help="Skip OCR, use cached pages only")
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Skip Qwen boundary detection (heuristic fallback only)",
    )
    parser.add_argument(
        "--refresh-boundaries",
        action="store_true",
        help="Re-run boundary detection even if cache exists",
    )
    parser.add_argument("--output-dir", default=None, help="Override output directory prefix")
    return parser.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)

    pdf_path = args.pdf
    if not Path(pdf_path).exists():
        logger.error(f"PDF not found: {pdf_path}")
        sys.exit(1)

    logger.info(f"{'='*60}")
    logger.info("Catalog Parser Starting")
    logger.info(f"PDF: {pdf_path}")
    logger.info(f"OCR model: {cfg.ocr.model}")
    logger.info(f"LLM model: {cfg.llm.model}")
    logger.info(f"{'='*60}")

    # ── Checkpoint ─────────────────────────────────────────────────────────
    checkpoint = None
    if cfg.processing.resume_from_checkpoint and not args.no_resume:
        checkpoint = load_checkpoint(cfg.paths.checkpoints_dir)
        if checkpoint and checkpoint.pdf_path != pdf_path:
            logger.warning("Checkpoint PDF mismatch — ignoring checkpoint")
            checkpoint = None
        if checkpoint:
            logger.info(
                f"Resuming from checkpoint: "
                f"{len(checkpoint.ocr_completed_pages)} OCR pages, "
                f"{checkpoint.products_extracted} products extracted"
            )

    from pypdf import PdfReader
    total_pages = len(PdfReader(pdf_path).pages)

    if checkpoint is None:
        checkpoint = CheckpointData(pdf_path=pdf_path, total_pages=total_pages)

    # ── Step 1: OCR ────────────────────────────────────────────────────────
    ocr_results = []

    if args.skip_ocr:
        logger.info("--skip-ocr: Loading cached OCR pages only")
        from src.utils.file_utils import load_ocr_page
        from src.models.product import PageOCRResult
        pages_done = list_ocr_pages(cfg.paths.ocr_dir)
        for p in pages_done:
            md = load_ocr_page(p, cfg.paths.ocr_dir)
            ocr_results.append(PageOCRResult(page_num=p, markdown=md or "", success=True))
        ocr_results.sort(key=lambda r: r.page_num)
    else:
        ocr = MistralOCR(cfg.ocr)
        ocr_results = ocr.process_pdf(
            pdf_path=pdf_path,
            ocr_dir=cfg.paths.ocr_dir,
            already_done=checkpoint.ocr_completed_pages,
        )
        checkpoint.ocr_completed_pages = [r.page_num for r in ocr_results if r.success]
        save_checkpoint(checkpoint, cfg.paths.checkpoints_dir)

    logger.info(f"OCR results: {len(ocr_results)} pages")

    # ── Step 2: Boundary Detection (Qwen pass) ───────────────────────────
    logger.info("Detecting product boundaries (Qwen)...")
    boundaries_cache = Path(cfg.paths.data_dir) / "boundaries.json"
    llm_cfg = None if args.rules_only else cfg.llm
    boundaries = detect_all_boundaries(
        ocr_results,
        llm_cfg,
        cfg.processing,
        cache_path=boundaries_cache,
        use_cache=not args.refresh_boundaries,
        pdf_path=pdf_path,
    )
    logger.info(f"Detected {len(boundaries)} product families")

    # ── Step 3: Product Extraction + Chunk Generation ──────────────────────
    logger.info("Extracting products and generating chunks...")
    parser = ProductParser(cfg.llm, cfg.processing)

    products_json_path = Path(cfg.paths.data_dir) / "products.json"
    products_json: list[dict] = []
    chunk_paths: list[str] = []

    if (
        checkpoint.products_extracted > 0
        and products_json_path.exists()
        and not args.no_resume
    ):
        try:
            products_json = json.loads(products_json_path.read_text(encoding="utf-8"))
            chunk_paths = [p.get("chunk_path", "") for p in products_json if p.get("chunk_path")]
            logger.info(f"Loaded {len(products_json)} previously extracted products")
        except Exception as exc:
            logger.warning(f"Could not load partial products.json: {exc}")
            products_json = []
            chunk_paths = []

    start_idx = checkpoint.products_extracted + 1
    remaining = boundaries[start_idx - 1 :] if start_idx > 1 else boundaries

    with tqdm(total=len(remaining), desc="Products", unit="product") as pbar:
        for offset, boundary in enumerate(remaining, start=start_idx):
            product = parser.parse(boundary, offset)
            chunk_path = generate_and_save_chunk(product, offset, cfg.paths.chunks_dir)
            chunk_paths.append(chunk_path)

            product_dict = product.model_dump()
            product_dict["chunk_path"] = chunk_path
            products_json.append(product_dict)

            checkpoint.products_extracted = offset
            save_checkpoint(checkpoint, cfg.paths.checkpoints_dir)
            save_products_json(products_json, cfg.paths.data_dir)

            pbar.update(1)
            pbar.set_postfix({"product": (product.product_name or "?")[:30]})

    # ── Step 4: Finalize ───────────────────────────────────────────────────
    clear_checkpoint(cfg.paths.checkpoints_dir)

    logger.info(f"{'='*60}")
    logger.info("Processing Complete!")
    logger.info(f"  Pages processed : {len(ocr_results)}")
    logger.info(f"  Products found  : {len(boundaries)}")
    logger.info(f"  Chunks saved    : {len(chunk_paths)}")
    logger.info(f"  Output dir      : {cfg.paths.chunks_dir}/")
    logger.info(f"  JSON manifest   : {cfg.paths.data_dir}/products.json")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
