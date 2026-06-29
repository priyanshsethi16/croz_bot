import os
import fitz
from openai import OpenAI

from extractors.base import BaseExtractor, ExtractionResult

PROMPT = """You are a document formatting assistant.
Convert the following raw PDF text into clean, structured Markdown.
Preserve headings, paragraphs, tables, bullet points and reading order.
Return Markdown only, no explanations."""


class QwenVLExtractor(BaseExtractor):
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv("OPENAI_RESPONSE_MODEL", "gpt-4.1-mini")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY missing in .env")
        self.client = OpenAI(api_key=api_key)

    def extract(self, pdf_path: str) -> ExtractionResult:
        try:
            doc = fitz.open(pdf_path)
            page_outputs = []

            for index, page in enumerate(doc, start=1):
                raw_text = page.get_text("text").strip()
                if not raw_text:
                    continue

                response = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": PROMPT},
                        {"role": "user", "content": f"Page {index}:\n\n{raw_text}"},
                    ],
                    max_tokens=4096,
                )

                page_outputs.append(f"\n\n## Page {index}\n\n{response.choices[0].message.content}")

            doc.close()
            markdown = "\n".join(page_outputs)

            return ExtractionResult(engine="qwen2_5_vl", text=markdown, markdown=markdown, success=True)

        except Exception as ex:
            return ExtractionResult(engine="qwen2_5_vl", success=False, error=str(ex))
