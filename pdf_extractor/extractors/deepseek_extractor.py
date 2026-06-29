"""DeepSeek-OCR extractor via local Ollama.

Renders each PDF page to a PNG image and sends it to the locally running
deepseek-ocr model through Ollama's OpenAI-compatible API.
"""
import base64
import os

import fitz  # PyMuPDF
from openai import OpenAI

from extractors.base import BaseExtractor, ExtractionResult

_OLLAMA_URL  = "http://localhost:11434/v1"
_OCR_PROMPT  = "<|grounding|>Convert the document to markdown."
_DPI         = 150


def _page_to_b64(page: fitz.Page) -> str:
    mat = fitz.Matrix(_DPI / 72, _DPI / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()


class DeepSeekOCRExtractor(BaseExtractor):
    def __init__(self):
        self.client = OpenAI(api_key="ollama", base_url=_OLLAMA_URL)
        self.model  = os.getenv("DEEPSEEK_OCR_MODEL", "deepseek-ocr")

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            doc   = fitz.open(pdf_path)
            pages = []

            for fitz_page in doc:
                b64 = _page_to_b64(fitz_page)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": b64}},
                            {"type": "text",      "text": _OCR_PROMPT},
                        ],
                    }],
                    max_tokens=4096,
                    temperature=0,
                )
                md = response.choices[0].message.content.strip()
                pages.append({"index": fitz_page.number, "markdown": md, "images": []})

            markdown = "\n\n".join(p["markdown"] for p in pages)
            return ExtractionResult(
                engine="deepseek_ocr",
                text=markdown,
                markdown=markdown,
                json_data={"pages": pages},
                success=True,
            )

        except Exception as ex:
            return ExtractionResult(engine="deepseek_ocr", success=False, error=str(ex))
