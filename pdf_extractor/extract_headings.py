#!/usr/bin/env python3
"""
Standalone script to extract headings from PDF using Mistral OCR.
Only extracts headings - no product identification or splitting.

Usage:
    python extract_headings.py --pdf input/catalog.pdf
    python extract_headings.py --pdf input/catalog.pdf --output headings.txt
"""

import os
import sys
import json
import argparse
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

try:
    from mistralai import Mistral
except ImportError:
    print("❌ mistralai package not installed. Run: pip install mistralai")
    sys.exit(1)

try:
    import fitz  # PyMuPDF
except ImportError:
    print("❌ PyMuPDF not installed. Run: pip install pymupdf")
    sys.exit(1)


def extract_headings_from_pdf(pdf_path: str, output_file: str = None):
    """Extract all headings from PDF using Mistral OCR."""
    
    # Check API key
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        print("❌ MISTRAL_API_KEY not found in .env file")
        sys.exit(1)
    
    client = Mistral(api_key=api_key)
    
    # Open PDF
    print(f"📄 Opening PDF: {pdf_path}")
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    print(f"   Total pages: {total_pages}")
    
    # Process all pages with OCR
    print(f"\n🔍 Running Mistral OCR on {total_pages} pages...")
    all_text = []
    
    for page_num in range(total_pages):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.pil_tobytes(format="PNG")
        
        print(f"   Page {page_num + 1}/{total_pages}...", end="\r")
        
        try:
            response = client.chat.complete(
                model="pixtral-12b-2409",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all text from this page in markdown format. Preserve headings with proper # symbols."},
                        {"type": "image_url", "image_url": f"data:image/png;base64,{img_bytes.hex()}"}
                    ]
                }]
            )
            
            page_text = response.choices[0].message.content
            all_text.append(f"\n\n--- PAGE {page_num + 1} ---\n\n{page_text}")
            
        except Exception as e:
            print(f"\n⚠️  Error on page {page_num + 1}: {e}")
            continue
    
    doc.close()
    print(f"\n✅ OCR completed for {total_pages} pages")
    
    # Combine all text
    full_text = "\n".join(all_text)
    
    # Extract headings
    print("\n📋 Extracting headings...")
    headings = []
    lines = full_text.split('\n')
    
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
        output_file = f"headings_{pdf_name}.txt"
    
    print(f"\n💾 Saving headings to: {output_file}")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(f"# Headings extracted from: {pdf_path}\n")
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
        description="Extract headings from PDF using Mistral OCR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python extract_headings.py --pdf input/catalog.pdf
  python extract_headings.py --pdf input/catalog.pdf --output my_headings.txt
        """
    )
    
    parser.add_argument(
        '--pdf',
        required=True,
        help='Path to input PDF file'
    )
    
    parser.add_argument(
        '--output',
        help='Output file path (default: headings_<pdfname>.txt)'
    )
    
    args = parser.parse_args()
    
    # Check if PDF exists
    if not os.path.exists(args.pdf):
        print(f"❌ PDF file not found: {args.pdf}")
        sys.exit(1)
    
    # Extract headings
    try:
        extract_headings_from_pdf(args.pdf, args.output)
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
