"""
pdf_to_images.py
----------------
Rasterizes PDF pages to PNG files using pdf2image.
Applies light image preprocessing (contrast + sharpening) to improve
vision model accuracy on small text and fractions.
Skips pages already on disk.
"""
from __future__ import annotations

from pathlib import Path

from tqdm import tqdm


def _enhance(image):
    """
    Apply a very light contrast boost and mild sharpening.
    Values are conservative — enough to help small text without
    distorting product photos or colour areas.
    """
    from PIL import ImageEnhance, ImageFilter

    # Contrast: 1.0 = original, 1.15 = 15% boost (very subtle)
    image = ImageEnhance.Contrast(image).enhance(1.15)

    # Sharpness: 1.0 = original, 1.3 = 30% boost (mild edge crispening)
    image = ImageEnhance.Sharpness(image).enhance(1.3)

    return image


def rasterize_pdf(
    pdf_path: str,
    pages_dir: str,
    dpi: int = 300,
    force: bool = False,
) -> list[Path]:
    """
    Convert every page of a PDF to a PNG file.

    Returns sorted list of PNG paths (one per page).
    Skips pages already rasterized unless force=True.
    """
    try:
        from pdf2image import convert_from_path
    except ImportError:
        raise ImportError(
            "pdf2image is required. Install with: pip install pdf2image\n"
            "Also install poppler: sudo apt-get install poppler-utils"
        )

    from pypdf import PdfReader
    total = len(PdfReader(pdf_path).pages)

    out_dir = Path(pages_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    png_paths: list[Path] = []
    pending: list[int] = []

    for page_num in range(1, total + 1):
        png_path = out_dir / f"page_{page_num:03d}.png"
        if png_path.exists() and not force:
            png_paths.append(png_path)
        else:
            pending.append(page_num)

    if pending:
        print(f"Rasterizing {len(pending)} pages at {dpi} DPI...")
        with tqdm(total=len(pending), desc="Rasterize", unit="page") as pbar:
            for page_num in pending:
                images = convert_from_path(
                    pdf_path,
                    dpi=dpi,
                    first_page=page_num,
                    last_page=page_num,
                    fmt="png",
                )
                if images:
                    png_path = out_dir / f"page_{page_num:03d}.png"
                    img = _enhance(images[0])
                    img.save(png_path, format="PNG")
                    png_paths.append(png_path)
                pbar.update(1)
    else:
        print(f"All {total} pages already rasterized — skipping.")

    # Return sorted by page number
    all_pngs = sorted(out_dir.glob("page_*.png"), key=lambda p: p.name)
    return all_pngs
