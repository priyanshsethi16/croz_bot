import os
import base64
try:
    from mistralai import Mistral
except ImportError:
    from mistralai.client import Mistral
from extractors.base import BaseExtractor, ExtractionResult


class MistralOCRExtractor(BaseExtractor):
    """Mistral OCR-4 only — pure Mistral output."""

    def __init__(self):
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY missing in .env")
        self.client = Mistral(api_key=api_key)

    def extract(self, pdf_path: str) -> ExtractionResult:
        return _run_mistral(self.client, pdf_path)


class MistralOnlyExtractor(BaseExtractor):
    """Mistral OCR-4 only — pure Mistral output."""

    def __init__(self):
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY missing in .env")
        self.client = Mistral(api_key=api_key)

    def extract(self, pdf_path: str) -> ExtractionResult:
        return _run_mistral(self.client, pdf_path)


def _run_mistral(client: Mistral, pdf_path: str) -> ExtractionResult:
    try:
        with open(pdf_path, "rb") as f:
            pdf_base64 = base64.b64encode(f.read()).decode("utf-8")

        response = client.ocr.process(
            model=os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-4"),
            document={
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{pdf_base64}"
            },
            include_image_base64=True
        )

        pages = []
        for page in response.pages:
            mistral_md = page.markdown or ""
            pages.append({**page.model_dump(), "markdown": mistral_md})

        markdown = "\n\n".join(p["markdown"] for p in pages)
        json_data = response.model_dump()
        json_data["pages"] = pages

        return ExtractionResult(
            engine="mistral_ocr",
            text=markdown,
            markdown=markdown,
            json_data=json_data,
            success=True
        )

    except Exception as ex:
        return ExtractionResult(
            engine="mistral_ocr",
            success=False,
            error=str(ex)
        )
