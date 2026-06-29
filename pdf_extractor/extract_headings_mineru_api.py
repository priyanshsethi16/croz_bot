#!/usr/bin/env python3
"""
Extract headings from PDF using MinerU Cloud API.
Uses the same API logic as mineru_extractor.py but only extracts headings.

Usage:
    python extract_headings_mineru_api.py --pdf input/catalog.pdf
    python extract_headings_mineru_api.py --pdf input/catalog.pdf --output headings.txt
"""

import os
import sys
import json
import time
import zipfile
import io
import argparse
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

try:
    import requests
except ImportError:
    print("❌ requests package not installed. Run: pip install requests")
    sys.exit(1)

_BASE_V4       = "https://mineru.net/api/v4"
_POLL_INTERVAL = 5    # seconds between status checks
_TIMEOUT       = 300  # max seconds to wait


def _headers():
    """Get API headers with authorization token."""
    token = os.getenv("MINERU_API_KEY")
    if not token:
        raise ValueError("❌ MINERU_API_KEY missing in .env file")
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _upload_and_get_task_id(pdf_path: str) -> str:
    """Upload PDF to MinerU and get batch_id."""
    filename = os.path.basename(pdf_path)
    
    # Use VLM model (95-96% accuracy, balanced)
    # Valid options: "vlm" (balanced, recommended), "auto" (API decides)
    # Note: "ocr" mentioned in docs but currently rejected by API
    model_version = os.getenv("MINERU_MODEL_VERSION", "vlm")
    
    print(f"📤 Uploading PDF to MinerU Cloud API...")
    print(f"   Model: {model_version}")
    print(f"   File: {filename}")
    
    # Step 1: Get presigned upload URL
    res = requests.post(
        f"{_BASE_V4}/file-urls/batch",
        headers=_headers(),
        json={"files": [{"name": filename}], "model_version": model_version},
    )
    res.raise_for_status()
    data = res.json()
    if data["code"] != 0:
        raise RuntimeError(f"MinerU upload URL failed: {data['msg']}")

    batch_id   = data["data"]["batch_id"]
    upload_url = data["data"]["file_urls"][0]
    
    print(f"   Batch ID: {batch_id}")

    # Step 2: Upload file
    print(f"   Uploading...", end="", flush=True)
    with open(pdf_path, "rb") as f:
        up = requests.put(upload_url, data=f)
    if up.status_code != 200:
        raise RuntimeError(f"MinerU file upload failed: {up.status_code}")
    
    print(" ✅")
    return batch_id


def _poll_batch(batch_id: str) -> str:
    """Poll batch status until done, return full_zip_url."""
    print(f"\n⏳ Waiting for MinerU to process (timeout: {_TIMEOUT}s)...")
    
    deadline = time.time() + _TIMEOUT
    dots = 0
    
    while time.time() < deadline:
        res = requests.get(
            f"{_BASE_V4}/extract-results/batch/{batch_id}",
            headers=_headers(),
        )
        res.raise_for_status()
        data = res.json().get("data", {})
        tasks = data.get("extract_result", [])

        if not tasks:
            print(".", end="", flush=True)
            dots += 1
            if dots % 10 == 0:
                print(f" {dots * _POLL_INTERVAL}s")
            time.sleep(_POLL_INTERVAL)
            continue

        task = tasks[0]
        state = task.get("state")

        if state == "done":
            print(" ✅\n")
            return task["full_zip_url"]
        elif state == "failed":
            raise RuntimeError(f"MinerU extraction failed: {task.get('err_msg')}")

        print(".", end="", flush=True)
        dots += 1
        if dots % 10 == 0:
            print(f" {dots * _POLL_INTERVAL}s")
        time.sleep(_POLL_INTERVAL)

    raise TimeoutError(f"MinerU extraction timed out after {_TIMEOUT}s")


def _extract_markdown_from_zip(zip_url: str) -> str:
    """Download zip and extract full.md content."""
    print("📥 Downloading result zip...")
    res = requests.get(zip_url)
    res.raise_for_status()
    
    print("📂 Extracting markdown from zip...")
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        for name in z.namelist():
            if name.endswith("full.md"):
                print(f"   Found: {name}")
                return z.read(name).decode("utf-8")
    
    raise RuntimeError("full.md not found in MinerU result zip")


def extract_headings(pdf_path: str, output_file: str = None):
    """Extract headings from PDF using MinerU Cloud API."""
    
    # Upload and get batch ID
    batch_id = _upload_and_get_task_id(pdf_path)
    
    # Poll until processing is done
    zip_url = _poll_batch(batch_id)
    
    # Download and extract markdown
    markdown = _extract_markdown_from_zip(zip_url)
    
    # Extract headings from markdown
    print("\n📋 Extracting headings from markdown...")
    headings = []
    lines = markdown.split('\n')
    
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        
        # Detect markdown headings
        if line.startswith('#'):
            level = 0
            text = line
            while text.startswith('#'):
                level += 1
                text = text[1:].strip()
            
            if text:  # Only add if there's actual text after the #
                headings.append({
                    'line_number': line_num,
                    'level': level,
                    'text': text
                })
    
    print(f"   Found {len(headings)} headings")
    
    # Save headings
    if output_file is None:
        pdf_name = Path(pdf_path).stem
        output_file = f"headings_mineru_{pdf_name}.txt"
    
    print(f"\n💾 Saving headings to: {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"# Headings extracted from: {pdf_path}\n")
        f.write(f"# Extraction method: MinerU Cloud API (VLM)\n")
        f.write(f"# Total headings: {len(headings)}\n")
        f.write(f"# Date: {__import__('datetime').datetime.now().isoformat()}\n\n")
        f.write("=" * 80 + "\n\n")
        
        for h in headings:
            indent = '  ' * (h['level'] - 1)
            f.write(f"Line {h['line_number']:4d}: {indent}{'#' * h['level']} {h['text']}\n")
    
    # Also save as JSON
    json_file = output_file.replace('.txt', '.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(headings, f, indent=2, ensure_ascii=False)
    
    print(f"   Text format: {output_file}")
    print(f"   JSON format: {json_file}")
    
    # Save full markdown for reference
    md_file = output_file.replace('.txt', '_full.md')
    with open(md_file, 'w', encoding='utf-8') as f:
        f.write(markdown)
    print(f"   Full markdown: {md_file}")
    
    # Print summary by level
    print("\n📊 Summary by heading level:")
    level_counts = {}
    for h in headings:
        level_counts[h['level']] = level_counts.get(h['level'], 0) + 1
    
    for level in sorted(level_counts.keys()):
        print(f"   Level {level}: {level_counts[level]} headings")
    
    # Show first 10 headings as preview
    print("\n👀 Preview (first 10 headings):")
    for h in headings[:10]:
        indent = '  ' * (h['level'] - 1)
        print(f"   {indent}{'#' * h['level']} {h['text']}")
    
    if len(headings) > 10:
        print(f"   ... and {len(headings) - 10} more")
    
    print("\n✅ Done!")
    return headings


def main():
    parser = argparse.ArgumentParser(
        description="Extract headings from PDF using MinerU Cloud API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  python extract_headings_mineru_api.py --pdf input/catalog.pdf
  
  # Custom output file
  python extract_headings_mineru_api.py --pdf input/catalog.pdf --output my_headings.txt

Environment Variables:
  MINERU_API_KEY          - Required: Your MinerU API key
  MINERU_MODEL_VERSION    - Optional: "vlm" (default, recommended), "auto" (API decides)
                            Note: "ocr" not currently supported by API
        """
    )
    
    parser.add_argument(
        '--pdf',
        required=True,
        help='Path to input PDF file'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path (default: headings_mineru_<pdfname>.txt)'
    )
    
    args = parser.parse_args()
    
    # Check if PDF exists
    if not os.path.exists(args.pdf):
        print(f"❌ PDF file not found: {args.pdf}")
        sys.exit(1)
    
    # Check API key
    if not os.getenv("MINERU_API_KEY"):
        print("❌ MINERU_API_KEY not found in .env file")
        print("   Add it to .env: MINERU_API_KEY=your_api_key_here")
        sys.exit(1)
    
    # Extract headings
    try:
        extract_headings(args.pdf, args.output)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
