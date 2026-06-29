"""
python app.py --pdf input/ADP.pdf                          # mistral OCR (default)
python app.py --pdf input/ADP.pdf --ocr deepseek           # use DeepSeek Vision OCR
python app.py --pdf input/ADP.pdf --llm groq               # use Groq for boundary LLM
python app.py --pdf input/ADP.pdf --llm none               # rules only, no LLM
"""
import argparse
import os
import re
import time
from dotenv import load_dotenv

from services.pdf_router import PDFExtractionRouter, _ENGINE_INFO as _OCR_INFO
from services.output_writer import OutputWriter
# from chunker.chunker import run_pipeline
from preprocessing import HeadingExtractor, ProductIdentifier

_LLM_CHOICES = ["mistral", "groq", "openai", "none"]

_LLM_INFO = {
    "mistral": "Mistral API  (MISTRAL_API_KEY)  — free 1B tokens/month",
    "groq":    "Groq API     (GROQ_API_KEY)     — free, very fast",
    "openai":  "OpenAI API   (OPENAI_API_KEY)   — paid",
    "none":    "Rules only   — no LLM call",
}


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Groz PDF → Product Chunks",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--pdf", required=True, help="Path to input PDF")
    parser.add_argument(
        "--ocr",
        choices=list(_OCR_INFO),
        default="mistral",
        help=(
            "OCR engine to use:\n"
            + "\n".join(f"  {k:10s} → {v}" for k, v in _OCR_INFO.items())
        ),
    )
    parser.add_argument(
        "--llm",
        choices=_LLM_CHOICES,
        default="mistral",
        help=(
            "LLM to use for uncertain boundary decisions:\n"
            + "\n".join(f"  {k:8s} → {v}" for k, v in _LLM_INFO.items())
        ),
    )
    args = parser.parse_args()

    # ── Info ─────────────────────────────────────────────────────────────────
    t_total = time.time()
    print(f"\n📄 PDF      : {args.pdf}")
    print(f"🔍 OCR      : {_OCR_INFO[args.ocr]}")
    print(f"🤖 LLM      : {_LLM_INFO[args.llm]}")

    # ── Step 1: OCR ──────────────────────────────────────────────────────────
    print(f"\n⏳ Running {args.ocr.title()} OCR...")
    t0 = time.time()
    router = PDFExtractionRouter()
    result = router.extract(args.pdf, engine=args.ocr)

    if not result.success:
        print("❌ Extraction failed:", result.error)
        return

    pdf_name = os.path.splitext(os.path.basename(args.pdf))[0]
    pdf_name = re.sub(r"[^\w\-]+", "_", pdf_name).strip("_")

    writer = OutputWriter(output_dir=f"output/{args.ocr}/{pdf_name}")
    files  = writer.save(result)
    print(f"   Markdown : {files['markdown']}")
    if 'html' in files:
        print(f"   HTML     : {files['html']}")
    print(f"   JSON     : {files['json']}")
    print(f"   ⏱  OCR took {time.time() - t0:.1f}s")

    # ── Step 1.5: Extract Headings (Preprocessing) ──────────────────────────
    print("\n🔍 Extracting headings...")
    heading_extractor = HeadingExtractor()
    headings_path = f"output/{args.ocr}/{pdf_name}/headings.txt"
    headings = heading_extractor.extract_and_save(result, headings_path)
    print(f"   Found {len(headings)} heading(s)")
    print(f"   Headings : {headings_path}")

    # ── Step 1.6: LLM Product Identification & Splitting ────────────────────
    print("\n🤖 Identifying Groz products using LLM...")
    product_identifier = ProductIdentifier(llm_provider="groq")
    chunks, manifest_path = product_identifier.process_and_split(
        headings=headings,
        full_text=result.markdown,
        output_dir=f"output/{args.ocr}/{pdf_name}"
    )
    print(f"   Created {len(chunks)} product chunk(s)")
    print(f"   Manifest : {manifest_path}")

    print(f"\n✅ Processing complete!")
    print(f"\n📁 Output files:")
    print(f"   OCR: output/{args.ocr}/{pdf_name}/")
    print(f"   Products: output/{args.ocr}/{pdf_name}/products/")
    print(f"\n⏱  Total time: {time.time() - t_total:.1f}s")


if __name__ == "__main__":
    main()
