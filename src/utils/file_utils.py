"""File I/O helpers."""
import json
import re
from pathlib import Path
from datetime import datetime

from src.models.product import CheckpointData
from src.utils.logger import logger


# ── OCR cache ──────────────────────────────────────────────────────────────

def save_ocr_page(page_num: int, markdown: str, ocr_dir: str = "data/ocr") -> Path:
    Path(ocr_dir).mkdir(parents=True, exist_ok=True)
    path = Path(ocr_dir) / f"page_{page_num:03d}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def load_ocr_page(page_num: int, ocr_dir: str = "data/ocr") -> str | None:
    path = Path(ocr_dir) / f"page_{page_num:03d}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def list_ocr_pages(ocr_dir: str = "data/ocr") -> list[int]:
    """Return sorted list of already-OCR'd page numbers."""
    pages = []
    for p in Path(ocr_dir).glob("page_*.md"):
        m = re.search(r"page_(\d+)\.md", p.name)
        if m:
            pages.append(int(m.group(1)))
    return sorted(pages)


# ── Chunk output ───────────────────────────────────────────────────────────

def save_chunk(index: int, slug: str, markdown: str, chunks_dir: str = "chunks") -> Path:
    Path(chunks_dir).mkdir(parents=True, exist_ok=True)
    filename = f"{index:04d}_{slug}.md"
    path = Path(chunks_dir) / filename
    path.write_text(markdown, encoding="utf-8")
    logger.debug(f"Saved chunk: {path}")
    return path


def save_products_json(products: list[dict], output_dir: str = "data") -> Path:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    path = Path(output_dir) / "products.json"
    path.write_text(json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved {len(products)} products → {path}")
    return path


# ── Checkpoint ─────────────────────────────────────────────────────────────

def save_checkpoint(data: CheckpointData, checkpoint_dir: str = "checkpoints") -> Path:
    Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)
    data.last_updated = datetime.now().isoformat()
    path = Path(checkpoint_dir) / "checkpoint.json"
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_checkpoint(checkpoint_dir: str = "checkpoints") -> CheckpointData | None:
    path = Path(checkpoint_dir) / "checkpoint.json"
    if not path.exists():
        return None
    try:
        return CheckpointData.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Checkpoint load failed: {e}")
        return None


def clear_checkpoint(checkpoint_dir: str = "checkpoints") -> None:
    path = Path(checkpoint_dir) / "checkpoint.json"
    if path.exists():
        path.unlink()