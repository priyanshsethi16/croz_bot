"""
Test script for heading extraction preprocessing.
Usage: python3 test_heading_extractor.py
"""
import sys
sys.path.insert(0, '.')

from preprocessing.heading_extractor import HeadingExtractor
from extractors.base import ExtractionResult


def test_with_sample_markdown():
    """Test with sample OCR markdown text."""
    
    sample_markdown = """
# Cordless Drill

## Features
- High performance motor
- Ergonomic design

## Specifications

### Technical Details
**Voltage**: 18V
**Speed**: 0-1500 RPM

### Battery Information
**Type**: Li-ion
**Capacity**: 2.0Ah

# Accessories

## Included Items
- Drill bits set
- Carrying case

## Optional Accessories
- Extra battery pack
- Charger stand
"""
    
    # Create mock ExtractionResult
    result = ExtractionResult(
        engine="test",
        markdown=sample_markdown,
        text=sample_markdown,
        success=True
    )
    
    # Extract headings
    extractor = HeadingExtractor()
    headings = extractor.extract_and_save(result, "output/test_headings.txt")
    
    print("✅ Heading extraction test completed!\n")
    print(f"Found {len(headings)} headings:\n")
    print(extractor.format_headings(headings))
    print(f"\n💾 Headings saved to: output/test_headings.txt")
    
    return headings


if __name__ == "__main__":
    import os
    os.makedirs("output", exist_ok=True)
    test_with_sample_markdown()
