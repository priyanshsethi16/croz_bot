import fitz
from extractors.base import BaseExtractor, ExtractionResult


class PyMuPDFExtractor(BaseExtractor):
    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            doc = fitz.open(pdf_path)
            pages = []
            full_pages = []

            for page in doc:
                text = page.get_text("text").strip()
                pages.append(f"## Page {page.number + 1}\n\n{text}")
                full_pages.append({"index": page.number, "markdown": text, "images": []})

            markdown = "\n\n".join(pages)
            return ExtractionResult(
                engine="pymupdf",
                text=markdown,
                markdown=markdown,
                json_data={"pages": full_pages},
                success=True
            )

        except Exception as ex:
            return ExtractionResult(
                engine="pymupdf",
                success=False,
                error=str(ex)
            )

    def extract_page(self, pdf_path: str, page_index: int) -> str:
        """Extract a single page by index, returns markdown text."""
        doc = fitz.open(pdf_path)
        page = doc[page_index]
        return page.get_text("text").strip()