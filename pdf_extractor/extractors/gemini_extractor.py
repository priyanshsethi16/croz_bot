"""Google Gemini 2.0 Flash Vision API for PDF text extraction."""
import os
import base64
from google import genai
from extractors.base import BaseExtractor, ExtractionResult


class GeminiExtractor(BaseExtractor):
    """Gemini 2.0 Flash - Google's latest multimodal model for PDF extraction."""

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY missing in .env")
        
        # Initialize new Google GenAI client
        self.client = genai.Client(api_key=api_key)
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            # Read PDF as base64
            with open(pdf_path, "rb") as f:
                pdf_base64 = base64.b64encode(f.read()).decode("utf-8")
            
            prompt = """Extract ALL text content from this PDF document with maximum accuracy.

REQUIREMENTS:
1. Preserve original structure and formatting
2. Extract all text including headers, tables, product codes, specifications
3. Maintain table formatting using markdown tables where possible
4. Include page numbers and section breaks
5. Preserve mathematical symbols and special characters
6. Extract text from images and diagrams if present

Output the content as clean, well-structured markdown."""

            # Generate content using new API
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=[
                    {
                        "parts": [
                            {"text": prompt},
                            {
                                "inline_data": {
                                    "mime_type": "application/pdf",
                                    "data": pdf_base64
                                }
                            }
                        ]
                    }
                ]
            )

            markdown_text = response.text.strip() if response.text else ""
            
            # Convert markdown to HTML
            try:
                import markdown as md_lib
                html = md_lib.markdown(markdown_text, extensions=['tables', 'fenced_code'])
            except ImportError:
                html = ""

            # Create pages structure
            pages = []
            page_chunks = markdown_text.split('\n\n\n') if markdown_text else ['']
            for i, chunk in enumerate(page_chunks):
                if chunk.strip():
                    pages.append({
                        "index": i,
                        "markdown": chunk.strip(),
                        "images": []
                    })

            return ExtractionResult(
                engine="gemini",
                text=markdown_text,
                markdown=markdown_text,
                html=html,
                json_data={"pages": pages},
                success=True
            )

        except Exception as ex:
            import traceback
            traceback.print_exc()
            return ExtractionResult(
                engine="gemini",
                success=False,
                error=str(ex)
            )