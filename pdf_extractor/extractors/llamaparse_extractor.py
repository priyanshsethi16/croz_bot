"""LlamaParse - LlamaIndex's premium PDF parsing service with structured output."""
import os
from llama_parse import LlamaParse
from extractors.base import BaseExtractor, ExtractionResult


class LlamaParseExtractor(BaseExtractor):
    """LlamaParse - Premium PDF parsing with table extraction and structured output."""

    def __init__(self):
        api_key = os.getenv("LLAMAPARSE_API_KEY")
        if not api_key:
            raise ValueError("LLAMAPARSE_API_KEY missing in .env")
        
        # Configure LlamaParse with best settings
        self.parser = LlamaParse(
            api_key=api_key,
            result_type="markdown",  # Can be "markdown" or "text"
            verbose=True,
            language="en",
            parsing_instruction="""
Extract all content with maximum accuracy:
- Preserve table structures using markdown format
- Maintain heading hierarchy
- Extract product codes, specifications, and technical details
- Include all text from images and diagrams
- Preserve mathematical symbols and units
- Keep original formatting where possible
            """.strip()
        )

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            print("   📤 Uploading to LlamaParse...", end="", flush=True)
            
            # Parse PDF
            documents = self.parser.load_data(pdf_path)
            print(" parsing...", end="", flush=True)
            
            # Combine all pages
            markdown_text = ""
            pages = []
            
            for i, doc in enumerate(documents):
                page_content = doc.text.strip()
                if page_content:
                    markdown_text += page_content + "\n\n"
                    pages.append({
                        "index": i,
                        "markdown": page_content,
                        "images": []
                    })
            
            print(" done")
            
            # Convert markdown to HTML
            try:
                import markdown as md_lib
                html = md_lib.markdown(markdown_text, extensions=['tables', 'fenced_code'])
            except ImportError:
                html = ""

            return ExtractionResult(
                engine="llamaparse",
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
                engine="llamaparse",
                success=False,
                error=str(ex)
            )