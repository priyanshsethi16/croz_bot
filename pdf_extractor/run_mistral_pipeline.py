"""
Runner called by the Django view via subprocess.
Prints a single JSON object to stdout:
  {"products": [...], "markdown": "...", "source_pdf": "..."}
All progress/debug output goes to stderr.
"""
import argparse
import json
import sys
import os
import re
from dotenv import load_dotenv

load_dotenv()

# Force non-thinking model — qwen3 thinking mode breaks JSON parsing
os.environ["GROQ_MODEL"] = "llama-3.3-70b-versatile"

from extractors.mistral_extractor import MistralOnlyExtractor
from preprocessing import HeadingExtractor, ProductIdentifier


_STRUCTURED_PROMPT = """You are extracting structured product data from a Groz industrial catalog page.

Given the raw OCR text below, extract ALL product information and return a JSON object.

RAW TEXT:
{raw_text}

Return ONLY a valid JSON object with this exact structure (use empty string/array/object if data not present):
{{
  "product_name": "Full product name",
  "product_code": "Product code/SKU if present, else empty string",
  "category": "Product category (e.g. Hammers, Drills, Lighting)",
  "description": "1-3 sentence product description",
  "features": ["feature 1", "feature 2"],
  "utilities": ["use case 1", "use case 2"],
  "specifications": {{"key": "value"}},
  "ordering_information": [
    {{"cat_no": "", "ord_no": "", "description": "", "size": "", "weight": ""}}
  ],
  "page_start": 1,
  "page_end": 1
}}

Return ONLY the JSON object, no other text."""


def _extract_structured(groq_client, model: str, chunk: dict) -> dict:
    raw_text = chunk.get("text", "")
    prompt = _STRUCTURED_PROMPT.format(raw_text=raw_text[:6000])
    try:
        response = groq_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a product catalog data extractor. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=2048,
        )
        result = response.choices[0].message.content.strip()
        result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()
        return json.loads(result)
    except Exception as e:
        print(f"   [warn] structured extraction failed: {e}", file=sys.stderr)
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    args = parser.parse_args()

    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(json.dumps({"error": f"File not found: {pdf_path}"}), file=sys.stdout)
        sys.exit(1)

    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    pdf_name = re.sub(r"[^\w\-]+", "_", pdf_name).strip("_")
    output_dir = f"output/mistral-only/{pdf_name}"

    # ── Step 1: OCR ───────────────────────────────────────────────────────────
    print("⏳ Running Mistral OCR...", file=sys.stderr)
    result = MistralOnlyExtractor().extract(pdf_path)
    if not result.success:
        print(json.dumps({"error": result.error}), file=sys.stdout)
        sys.exit(1)
    print(f"   OCR done ({len(result.markdown)} chars)", file=sys.stderr)

    # ── Step 1.5: Extract Headings ────────────────────────────────────────────
    headings_path = f"{output_dir}/headings.txt"
    headings = HeadingExtractor().extract_and_save(result, headings_path)
    print(f"   {len(headings)} heading(s) found", file=sys.stderr)

    # ── Step 1.6: LLM Product Identification & Splitting (same as app.py) ─────
    print("🤖 Identifying products...", file=sys.stderr)
    identifier = ProductIdentifier(llm_provider="groq")
    chunks, manifest_path = identifier.process_and_split(
        headings=headings,
        full_text=result.markdown,
        output_dir=output_dir,
        print_fn=lambda msg: print(msg, file=sys.stderr),
    )
    print(f"   {len(chunks)} product(s) identified", file=sys.stderr)

    # ── Step 2: Structured extraction via Groq ────────────────────────────────
    print("🔬 Extracting structured fields...", file=sys.stderr)
    try:
        from groq import Groq
        groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
        struct_model = "llama-3.3-70b-versatile"
    except Exception as e:
        print(f"   [warn] Groq unavailable: {e}", file=sys.stderr)
        groq_client = None
        struct_model = None

    # ── Build output ──────────────────────────────────────────────────────────
    products = []
    for chunk in chunks:
        structured = _extract_structured(groq_client, struct_model, chunk) if groq_client else {}

        ordering_rows = structured.get("ordering_information", [])
        children = []
        if ordering_rows and isinstance(ordering_rows, list):
            for row in ordering_rows:
                if isinstance(row, dict):
                    children.append({
                        "product_name": structured.get("product_name") or chunk["product_name"],
                        "product_code": row.get("cat_no", ""),
                        "ordering_table": [row],
                        "specifications": structured.get("specifications", {}),
                    })

        products.append({
            "product_name":   structured.get("product_name") or chunk["product_name"],
            "product_code":   structured.get("product_code", ""),
            "category":       structured.get("category", ""),
            "description":    structured.get("description", ""),
            "features":       structured.get("features", []),
            "utilities":      structured.get("utilities", []),
            "specifications": structured.get("specifications", {}),
            "children":       children,
            "page_start":     structured.get("page_start", 1),
            "page_end":       structured.get("page_end", 1),
            "raw_text":       chunk["text"],
            "_chunk_text":    chunk["text"],
        })

    print(json.dumps({
        "products":   products,
        "markdown":   "",
        "source_pdf": os.path.basename(pdf_path),
    }, ensure_ascii=False), file=sys.stdout)


if __name__ == "__main__":
    main()
