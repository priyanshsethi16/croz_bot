#!/usr/bin/env python3
"""
Standalone script to extract headings from PDF using MinerU.
Only extracts headings - no product identification or splitting.

Usage:
    python extract_headings_mineru.py --pdf input/catalog.pdf
    python extract_headings_mineru.py --pdf input/catalog.pdf --output headings.txt
"""

import os
import sys
import json
import argparse
from pathlib import Path

try:
    from magic_pdf.pipe.UNIPipe import UNIPipe
    from magic_pdf.pipe.OCRPipe import OCRPipe
    from magic_pdf.rw.DiskReaderWriter import DiskReaderWriter
except ImportError:
    print("❌ MinerU (magic-pdf) not installed. Run: pip install magic-pdf[full]")
    sys.exit(1)


def extract_headings_with_mineru(pdf_path: str, output_file: str = None, use_ocr: bool = False):
    """Extract all headings from PDF using MinerU."""
    
    print(f"📄 Opening PDF: {pdf_path}")
    
    # Read PDF bytes
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()
    
    # Setup output directory
    pdf_name = Path(pdf_path).stem
    temp_output_dir = f"temp_mineru_{pdf_name}"
    os.makedirs(temp_output_dir, exist_ok=True)
    
    print(f"🔍 Processing PDF with MinerU...")
    print(f"   Using {'OCR mode' if use_ocr else 'Text extraction mode'}")
    
    try:
        # Create reader/writer
        reader_writer = DiskReaderWriter(temp_output_dir)
        
        # Choose pipeline based on mode
        if use_ocr:
            pipe = OCRPipe(pdf_bytes, reader_writer)
        else:
            pipe = UNIPipe(pdf_bytes, reader_writer)
        
        # Execute pipeline
        pipe.pipe_classify()
        pipe.pipe_analyze()
        pipe.pipe_parse()
        
        # Get parsed content
        content_list = pipe.pipe_mk_uni_format(pdf_path, drop_mode="none")
        md_content = pipe.pipe_mk_markdown(pdf_path, drop_mode="none")
        
        print(f"✅ MinerU processing complete")
        
        # Extract headings from markdown
        print("\n📋 Extracting headings from markdown...")
        headings = []
        lines = md_content.split('\n')
        
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
            output_file = f"headings_mineru_{pdf_name}.txt"
        
        print(f"\n💾 Saving headings to: {output_file}")
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(f"# Headings extracted from: {pdf_path}\n")
            f.write(f"# Extraction method: MinerU {'(OCR)' if use_ocr else '(Text)'}\n")
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
            f.write(md_content)
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
        
        # Cleanup temp directory
        import shutil
        try:
            shutil.rmtree(temp_output_dir)
            print(f"🧹 Cleaned up temp directory: {temp_output_dir}")
        except:
            print(f"⚠️  Could not clean up temp directory: {temp_output_dir}")
        
        return headings
        
    except Exception as e:
        print(f"\n❌ Error during MinerU processing: {e}")
        import traceback
        traceback.print_exc()
        
        # Cleanup on error
        import shutil
        try:
            shutil.rmtree(temp_output_dir)
        except:
            pass
        
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Extract headings from PDF using MinerU",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage (text extraction)
  python extract_headings_mineru.py --pdf input/catalog.pdf
  
  # With OCR mode (for scanned PDFs)
  python extract_headings_mineru.py --pdf input/catalog.pdf --ocr
  
  # Custom output file
  python extract_headings_mineru.py --pdf input/catalog.pdf --output my_headings.txt
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
    
    parser.add_argument(
        '--ocr',
        action='store_true',
        help='Use OCR mode for scanned PDFs (slower but more accurate for images)'
    )
    
    args = parser.parse_args()
    
    # Check if PDF exists
    if not os.path.exists(args.pdf):
        print(f"❌ PDF file not found: {args.pdf}")
        sys.exit(1)
    
    # Extract headings
    try:
        extract_headings_with_mineru(args.pdf, args.output, args.ocr)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
