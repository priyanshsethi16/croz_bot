"""Entry point: run product chunking pipeline on a Mistral OCR JSON file.

Usage:
  python chunk_pipeline.py --ocr output/mistral/mistral_ocr.json
  python chunk_pipeline.py --ocr output/mistral/mistral_ocr.json --out output/chunks/chunks.json --no-qwen
"""
import argparse
import json
from dotenv import load_dotenv

from chunker.chunker import run_pipeline


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Groz product chunker")
    parser.add_argument("--ocr", required=True, help="Path to Mistral OCR JSON file")
    parser.add_argument("--out", default="output/chunks/product_chunks.json", help="Output JSON path")
    parser.add_argument("--no-qwen", action="store_true", help="Disable Qwen fallback (rules only)")
    args = parser.parse_args()

    chunks = run_pipeline(
        ocr_json_path=args.ocr,
        output_path=args.out,
        use_qwen_fallback=not args.no_qwen,
    )

    print(f"\n✅ Found {len(chunks)} product chunk(s):\n")
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{i}] {chunk.product_name}")
        print(f"       Pages     : {chunk.pages}")
        print(f"       Codes     : {chunk.product_codes}")
        print(f"       Has Spec  : {chunk.has_spec} | Has Ordering: {chunk.has_ordering}")
        print()

    print(f"Chunks saved to: {args.out}")


if __name__ == "__main__":
    main()
