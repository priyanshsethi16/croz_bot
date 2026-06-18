"""Mistral OCR layer — PDF page → markdown via Mistral OCR API."""
import base64
import io
import time
from pathlib import Path

import httpx
from pypdf import PdfReader
from tqdm import tqdm

from src.config import OCRConfig
from src.models.product import PageOCRResult
from src.utils.file_utils import save_ocr_page, load_ocr_page, list_ocr_pages
from src.utils.logger import logger

# pdf2image is used to rasterize pages to PNG for better table recognition.
try:
    from pdf2image import convert_from_path
    _PDF2IMAGE_AVAILABLE = True
except ImportError:
    _PDF2IMAGE_AVAILABLE = False
    logger.warning(
        "pdf2image not installed — falling back to single-page PDF extraction. "
        "Install with: pip install pdf2image"
    )


# Minimum ratio of pipe characters per line to consider a page table-heavy
_TABLE_PIPE_THRESHOLD = 0.15   # ≥15 % of non-empty lines contain "|"
_HIGH_DPI = 300                # DPI used on auto-retry for table-heavy pages
_DEFAULT_DPI = 300            # Standard rasterisation DPI


class MistralOCR:
    API_URL = "https://api.mistral.ai/v1/ocr"

    def __init__(self, config: OCRConfig):
        self.config = config
        if not config.mistral_api_key:
            raise ValueError("MISTRAL_API_KEY is not set.")
        self.headers = {
            "Authorization": f"Bearer {config.mistral_api_key}",
            "Content-Type": "application/json",
        }

    # ── Internal helpers ───────────────────────────────────────────────────

    def _rasterize_page_to_png(self, pdf_path: str, page_num: int, dpi: int = _DEFAULT_DPI) -> bytes:
        """Rasterize a single PDF page to PNG bytes using pdf2image."""
        images = convert_from_path(
            pdf_path,
            dpi=dpi,
            first_page=page_num,
            last_page=page_num,
            fmt="png",
        )
        if not images:
            raise ValueError(f"pdf2image returned no images for page {page_num}")
        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return buf.getvalue()

    def _extract_single_page_pdf(self, pdf_path: str, page_num: int) -> bytes:
        """Fallback: extract a single page as a PDF bytes object (used when pdf2image unavailable)."""
        from pypdf import PdfWriter
        reader = PdfReader(pdf_path)
        writer = PdfWriter()
        writer.add_page(reader.pages[page_num - 1])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    @staticmethod
    def _has_tables(markdown: str) -> bool:
        """Return True if the markdown looks table-heavy (many pipe-delimited rows)."""
        lines = [l for l in markdown.splitlines() if l.strip()]
        if not lines:
            return False
        pipe_lines = sum(1 for l in lines if "|" in l)
        return (pipe_lines / len(lines)) >= _TABLE_PIPE_THRESHOLD

    def _call_mistral_ocr(self, image_data: bytes, *, is_png: bool = True) -> str:
        """Call Mistral OCR API with image or PDF bytes. Returns markdown string."""
        b64 = base64.b64encode(image_data).decode("utf-8")
        if is_png:
            # Images use "image_url" type with image/png data URI
            document = {
                "type": "image_url",
                "image_url": f"data:image/png;base64,{b64}",
            }
        else:
            # PDFs use "document_url" type with application/pdf data URI
            document = {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{b64}",
            }

        payload = {
            "model": self.config.model,
            "document": document,
            "include_image_base64": False,
            # No table_format specified — Mistral returns tables inline in
            # markdown by default, which is what we want for downstream parsing.
        }

        for attempt in range(1, self.config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.config.timeout) as client:
                    resp = client.post(self.API_URL, headers=self.headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    # Mistral OCR response: pages[].markdown
                    # With no table_format set, tables are inline in markdown.
                    pages = data.get("pages", [])
                    if pages:
                        return "\n\n".join(p.get("markdown", "") for p in pages)
                    return data.get("text", "") or ""
            except httpx.HTTPStatusError as e:
                logger.warning(f"OCR HTTP {e.response.status_code} attempt {attempt}: {e}")
                if e.response.status_code == 429:
                    time.sleep(self.config.retry_delay * attempt * 2)
                elif attempt == self.config.max_retries:
                    raise
                else:
                    time.sleep(self.config.retry_delay * attempt)
            except Exception as e:
                logger.warning(f"OCR error attempt {attempt}: {e}")
                if attempt == self.config.max_retries:
                    raise
                time.sleep(self.config.retry_delay * attempt)

        return ""

    def _ocr_page_with_retry(self, pdf_path: str, page_num: int) -> str:
        """
        OCR one page.

        1. Rasterize at DEFAULT_DPI → call Mistral.
        2. If result looks table-heavy (many pipe rows present but may be
           mis-parsed), re-rasterize at HIGH_DPI and call again.
        3. Keep whichever result has more pipe-delimited lines (better table
           fidelity), or simply return the high-DPI result.
        Falls back to single-page PDF extraction when pdf2image is unavailable.
        """
        if _PDF2IMAGE_AVAILABLE:
            # --- PNG path (preferred) ---
            png_bytes = self._rasterize_page_to_png(pdf_path, page_num, dpi=_DEFAULT_DPI)
            markdown = self._call_mistral_ocr(png_bytes, is_png=True)

            if self._has_tables(markdown):
                logger.debug(f"Page {page_num}: table detected — retrying at {_HIGH_DPI} DPI")
                png_hd = self._rasterize_page_to_png(pdf_path, page_num, dpi=_HIGH_DPI)
                markdown_hd = self._call_mistral_ocr(png_hd, is_png=True)
                # Prefer higher-DPI result (more detail → better table rows)
                markdown = markdown_hd or markdown
        else:
            # --- PDF fallback path ---
            pdf_bytes = self._extract_single_page_pdf(pdf_path, page_num)
            markdown = self._call_mistral_ocr(pdf_bytes, is_png=False)

        return markdown

    # ── Public API ─────────────────────────────────────────────────────────

    def process_page(self, pdf_path: str, page_num: int, ocr_dir: str = "data/ocr") -> PageOCRResult:
        """OCR a single page; returns cached result if available."""
        cached = load_ocr_page(page_num, ocr_dir)
        if cached is not None:
            logger.debug(f"Page {page_num}: cache hit")
            return PageOCRResult(page_num=page_num, markdown=cached, success=True)

        try:
            markdown = self._ocr_page_with_retry(pdf_path, page_num)
            save_ocr_page(page_num, markdown, ocr_dir)
            return PageOCRResult(page_num=page_num, markdown=markdown, success=True)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Page {page_num} OCR failed: {err_msg}")
            save_ocr_page(page_num, f"<!-- OCR_FAILED: {err_msg} -->", ocr_dir)
            return PageOCRResult(page_num=page_num, markdown="", success=False, error=err_msg)

    def process_pdf(
        self,
        pdf_path: str,
        ocr_dir: str = "data/ocr",
        already_done: list[int] | None = None,
    ) -> list[PageOCRResult]:
        """OCR all pages of a PDF with progress bar. Skips already-done pages."""
        reader = PdfReader(pdf_path)
        total = len(reader.pages)
        done_set = set(already_done or list_ocr_pages(ocr_dir))

        results: list[PageOCRResult] = []
        pending = [p for p in range(1, total + 1) if p not in done_set]

        logger.info(f"Total pages: {total} | Already OCR'd: {len(done_set)} | Pending: {len(pending)}")

        # First load cached pages
        for p in sorted(done_set):
            if p <= total:
                cached = load_ocr_page(p, ocr_dir)
                results.append(PageOCRResult(page_num=p, markdown=cached or "", success=True))

        # OCR pending pages
        with tqdm(total=len(pending), desc="OCR pages", unit="page") as pbar:
            for page_num in pending:
                result = self.process_page(pdf_path, page_num, ocr_dir)
                results.append(result)
                pbar.update(1)
                pbar.set_postfix({"page": page_num, "ok": result.success})

        # Sort by page number
        results.sort(key=lambda r: r.page_num)
        success_count = sum(1 for r in results if r.success)
        logger.info(f"OCR complete: {success_count}/{total} pages successful")
        return results
