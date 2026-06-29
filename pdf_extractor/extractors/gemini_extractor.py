import os
from google import genai
from extractors.base import BaseExtractor, ExtractionResult


class GeminiExtractor(BaseExtractor):
    def __init__(self, model: str = "gemini-2.5-pro"):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY missing in .env")

        self.client = genai.Client(api_key=api_key)
        self.model = model

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            uploaded_file = self.client.files.upload(file=pdf_path)

            prompt = """
Extract all content from this PDF.

Return output in Markdown format.
Preserve:
- headings
- paragraphs
- tables
- bullet lists
- page breaks
- image captions if visible
- reading order

Do not summarize. Extract the actual text.
"""

            response = self.client.models.generate_content(
                model=self.model,
                contents=[uploaded_file, prompt]
            )

            markdown = response.text or ""

            return ExtractionResult(
                engine="gemini",
                text=markdown,
                markdown=markdown,
                success=True
            )

        except Exception as ex:
            return ExtractionResult(
                engine="gemini",
                success=False,
                error=str(ex)
            )