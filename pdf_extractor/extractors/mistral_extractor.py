import os
import base64
from mistralai import Mistral
from extractors.base import BaseExtractor, ExtractionResult
from extractors.pymupdf_extractor import PyMuPDFExtractor

# If Mistral extracts fewer than this many chars on a page, fallback to PyMuPDF
_MIN_PAGE_CHARS = 100


def _merge_pymupdf_into_mistral(mistral_md: str, pymupdf_md: str, pdf_path: str = None, page_index: int = 0) -> str:
    """
    Insert PyMuPDF-only lines into Mistral markdown using spatial positioning.
    Lines from the right half of the page (x > 50% width) are variant/accessory codes
    and get inserted after their nearest product heading based on vertical position.
    Lines from left half that Mistral missed get appended to matching heading block.
    """
    import re
    import fitz

    mistral_lower = mistral_md.lower()

    # Collect lines PyMuPDF found that Mistral missed
    extra_lines = []
    for line in pymupdf_md.splitlines():
        line = line.strip()
        if len(line) < 3:
            continue
        if line.lower() not in mistral_lower:
            extra_lines.append(line)

    if not extra_lines:
        return mistral_md

    # If no pdf_path, fall back to appending
    if not pdf_path:
        return mistral_md + "\n\n" + "\n".join(extra_lines)

    # Get positioned words from PyMuPDF
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    page_width = page.rect.width

    # Build map: text -> x position (normalized 0-1)
    word_positions = {}
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        word_positions[word.strip()] = x0 / page_width

    # Split Mistral into heading blocks
    heading_re = re.compile(r"^(#{1,3} .+)$", re.MULTILINE)
    splits = [(m.start(), m.group(1)) for m in heading_re.finditer(mistral_md)]

    if not splits:
        return mistral_md + "\n\n" + "\n".join(extra_lines)

    # Separate: right-column lines (x > 0.45) vs left-column missed lines
    right_col_lines = []
    left_col_lines = []
    for line in extra_lines:
        words = re.split(r"[\s/\-]+", line)
        positions = [word_positions.get(w, 0.5) for w in words if w in word_positions]
        avg_x = sum(positions) / len(positions) if positions else 0.5
        if avg_x > 0.45:
            right_col_lines.append(line)
        else:
            left_col_lines.append(line)

    # Right-column lines: insert after the last heading block (they are variant codes)
    # Left-column lines: append at end of relevant heading
    result = mistral_md

    # Insert right-column variant codes after each major product heading
    # Group them and append after the last heading's content
    if right_col_lines:
        last_heading_pos = splits[-1][0] if splits else 0
        # Find end of last heading block
        insert_pos = len(result)
        insert_text = "\n\n" + "\n".join(right_col_lines)
        # Insert before end
        result = result[:insert_pos] + insert_text

    if left_col_lines:
        result = result + "\n\n" + "\n".join(left_col_lines)

    return result


class MistralOCRExtractor(BaseExtractor):
    """Mistral OCR + PyMuPDF merge — recovers colored box text Mistral misses."""

    def __init__(self):
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY missing in .env")
        self.client = Mistral(api_key=api_key)

    def extract(self, pdf_path: str) -> ExtractionResult:
        return _run_mistral(self.client, pdf_path, use_pymupdf=True)


class MistralOnlyExtractor(BaseExtractor):
    """Mistral OCR only — no PyMuPDF merge."""

    def __init__(self):
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY missing in .env")
        self.client = Mistral(api_key=api_key)

    def extract(self, pdf_path: str) -> ExtractionResult:
        return _run_mistral(self.client, pdf_path, use_pymupdf=False)


def _run_mistral(client: Mistral, pdf_path: str, use_pymupdf: bool) -> ExtractionResult:
    try:
        with open(pdf_path, "rb") as f:
            pdf_base64 = base64.b64encode(f.read()).decode("utf-8")

        model = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
        
        response = client.ocr.process(
            model=model,
            document={
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{pdf_base64}"
            },
            include_image_base64=True
        )

        pymupdf = PyMuPDFExtractor() if use_pymupdf else None
        pages = []
        pages_html = []
        fallback_count = 0

        for page in response.pages:
            mistral_md = page.markdown or ""
            mistral_html = getattr(page, 'html', '') or ''

            if use_pymupdf:
                pymupdf_md = pymupdf.extract_page(pdf_path, page.index)
                if len(mistral_md.strip()) < _MIN_PAGE_CHARS:
                    merged = pymupdf_md if pymupdf_md else mistral_md
                    fallback_count += 1
                else:
                    merged = _merge_pymupdf_into_mistral(mistral_md, pymupdf_md, pdf_path, page.index)
            else:
                merged = mistral_md

            page_dict = {**page.model_dump(), "markdown": merged}
            if mistral_html:
                page_dict["html"] = mistral_html
            pages.append(page_dict)
            if mistral_html:
                pages_html.append({**page.model_dump(), "html": mistral_html})

        if fallback_count:
            print(f"   ⚠️  PyMuPDF full-fallback used on {fallback_count} page(s) (Mistral got <{_MIN_PAGE_CHARS} chars)")

        markdown_text = "\n\n".join(p["markdown"] for p in pages)
        
        # Convert markdown to HTML
        try:
            import markdown as md_lib
            html = md_lib.markdown(markdown_text, extensions=['tables', 'fenced_code'])
        except ImportError:
            html = ""  # markdown library not installed
        
        json_data = response.model_dump()
        json_data["pages"] = pages

        return ExtractionResult(
            engine="mistral_ocr",
            text=markdown_text,
            markdown=markdown_text,
            html=html,
            json_data=json_data,
            success=True
        )

    except Exception as ex:
        import traceback
        traceback.print_exc()
        return ExtractionResult(
            engine="mistral_ocr",
            success=False,
            error=str(ex)
        )
