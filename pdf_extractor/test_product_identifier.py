"""
Test script for LLM-based product identification and text splitting.
Usage: python3 test_product_identifier.py
"""
import sys
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

sys.path.insert(0, '.')

from preprocessing.heading_extractor import HeadingExtractor
from preprocessing.product_identifier import ProductIdentifier
from extractors.base import ExtractionResult


def test_product_identification():
    """Test with sample catalog markdown text."""
    
    sample_markdown = """
# GROZ TOOLS CATALOG 2024

## Table of Contents

# HAMMERS

## Club Hammers

**Product Code:** CHID

### Features
- Drop-forged steel head
- Hardened striking face
- Lacquered wooden handle

### Specifications
- **Standard:** DIN 1042
- **Material:** Carbon steel
- **Finish:** Lacquered

### Ordering Information
| Cat No | Weight | Length |
|--------|--------|--------|
| CH500  | 500g   | 280mm  |
| CH1000 | 1000g  | 320mm  |

## Sledge Hammers

**Product Code:** SHID

### Description
Heavy-duty sledge hammer for demolition work.

### Features
- Extra-long handle for maximum impact
- Fiberglass core for shock absorption

### Specifications
- **Weight Range:** 3-10 kg
- **Handle Material:** Fiberglass

# SCREWDRIVERS

## Precision Screwdriver Set

**Product Code:** PSID

### Description
15-piece precision screwdriver set for electronics work.

### Contents
- Phillips head: PH000, PH00, PH0
- Flat head: 1.0, 1.5, 2.0mm
- Torx: T5, T6, T7, T8

### Specifications
- **Blade Material:** Chrome vanadium steel
- **Handle:** ESD-safe plastic

# MEASURING TOOLS

## Digital Caliper

**Product Code:** DCID

### Features
- LCD display with backlight
- Metric and imperial units
- Data output port

### Specifications
- **Range:** 0-150mm / 0-6"
- **Accuracy:** ±0.02mm
- **Battery:** CR2032

## Vernier Caliper

**Product Code:** VCID

### Description
Traditional vernier caliper with fine adjustment.

### Specifications
- **Range:** 0-200mm
- **Resolution:** 0.05mm
"""
    
    # Create mock ExtractionResult
    result = ExtractionResult(
        engine="test",
        markdown=sample_markdown,
        text=sample_markdown,
        success=True
    )
    
    # Step 1: Extract headings
    print("=" * 70)
    print("STEP 1: EXTRACTING HEADINGS")
    print("=" * 70)
    heading_extractor = HeadingExtractor()
    headings = heading_extractor.extract_headings(result.markdown)
    
    print(f"\nFound {len(headings)} headings:\n")
    print(heading_extractor.format_headings(headings))
    
    # Step 2: LLM identifies actual products
    print("\n" + "=" * 70)
    print("STEP 2: LLM IDENTIFYING GROZ PRODUCTS")
    print("=" * 70)
    
    # Check if Groq API key is available
    if not os.getenv("GROQ_API_KEY"):
        print("\n⚠️  GROQ_API_KEY not found in .env")
        print("Skipping LLM identification test.\n")
        print("To run full test, add GROQ_API_KEY to .env file")
        return
    
    try:
        product_identifier = ProductIdentifier(llm_provider="groq")
        chunks, manifest_path = product_identifier.process_and_split(
            headings=headings,
            full_text=result.markdown,
            output_dir="output/test"
        )
        
        # Step 3: Display results
        print("\n" + "=" * 70)
        print("STEP 3: PRODUCT CHUNKS CREATED")
        print("=" * 70)
        
        for i, chunk in enumerate(chunks, 1):
            print(f"\n[{i}] {chunk['product_name']}")
            print(f"    Lines: {chunk['start_line']}-{chunk['end_line']}")
            print(f"    Characters: {chunk['char_count']}")
            print(f"    Preview: {chunk['text'][:100]}...")
        
        print(f"\n✅ Success! Created {len(chunks)} product chunks")
        print(f"📁 Files saved in: output/test/products/")
        print(f"📄 Manifest: {manifest_path}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_product_identification()
