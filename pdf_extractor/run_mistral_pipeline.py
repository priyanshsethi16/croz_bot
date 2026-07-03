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
from dotenv import load_dotenv

load_dotenv()

from extractors.mistral_extractor import MistralOnlyExtractor
from preprocessing import HeadingExtractor, ProductIdentifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    args = parser.parse_args()

    pdf_path = args.pdf
    if not os.path.exists(pdf_path):
        print(json.dumps({"error": f"File not found: {pdf_path}"}), file=sys.stdout)
        sys.exit(1)

    # ── OCR ──────────────────────────────────────────────────────────────────
    print("⏳ Running Mistral OCR...", file=sys.stderr)
    result = MistralOnlyExtractor().extract(pdf_path)

    if not result.success:
        print(json.dumps({"error": result.error}), file=sys.stdout)
        sys.exit(1)

    print(f"   OCR done ({len(result.markdown)} chars)", file=sys.stderr)

    # ── Headings ─────────────────────────────────────────────────────────────
    headings = HeadingExtractor().extract_headings(result.markdown)
    print(f"   {len(headings)} heading(s) found", file=sys.stderr)

    # ── Product identification ────────────────────────────────────────────────
    print("🤖 Identifying products...", file=sys.stderr)
    # Force a non-thinking model — qwen3-32b thinking mode breaks JSON parsing
    os.environ["GROQ_MODEL"] = "llama-3.3-70b-versatile"
    identifier = ProductIdentifier(llm_provider="groq")
    products_raw = identifier.identify_products(headings)
    chunks = identifier.split_text_by_products(result.markdown, products_raw)
    print(f"   {len(chunks)} product(s) identified", file=sys.stderr)

    # ── Build output matching _build_assembled_from_mistral expectations ──────
    products = []
    for chunk in chunks:
        products.append({
            "product_name": chunk["product_name"],
            "product_code": "",
            "category": "",
            "description": chunk["text"][:500],
            "features": [],
            "utilities": [],
            "specifications": {},
            "children": [],
            "page_start": 1,
            "page_end": 1,
            "raw_text": chunk["text"],  # only this product's text, not full PDF
            "_chunk_text": chunk["text"],
        })

    output = {
        "products": products,
        "markdown": "",  # omit full markdown — each product already has its own raw_text
        "source_pdf": os.path.basename(pdf_path),
    }

    print(json.dumps(output, ensure_ascii=False), file=sys.stdout)


if __name__ == "__main__":
    main()
