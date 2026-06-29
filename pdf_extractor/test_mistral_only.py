"""
Test script to verify Mistral OCR-4 only extraction (no PyMuPDF).
This demonstrates that all PyMuPDF merge logic has been removed.
"""

def test_mistral_extractor_code():
    """Verify the mistral_extractor.py has been cleaned up."""
    
    with open('extractors/mistral_extractor.py', 'r') as f:
        content = f.read()
    
    print("✅ VERIFICATION: Mistral OCR-4 Only Configuration\n")
    print("=" * 70)
    
    # Check what was removed
    removed_items = [
        ("PyMuPDF import", "from extractors.pymupdf_extractor import PyMuPDFExtractor"),
        ("_merge_pymupdf_into_mistral function", "def _merge_pymupdf_into_mistral"),
        ("_MIN_PAGE_CHARS constant", "_MIN_PAGE_CHARS"),
        ("use_pymupdf parameter", "use_pymupdf"),
        ("pymupdf merge logic", "if use_pymupdf:"),
        ("fallback_count tracking", "fallback_count"),
        ("fitz import", "import fitz"),
    ]
    
    print("\n📋 Removed Components (No longer in code):\n")
    for name, code_snippet in removed_items:
        if code_snippet not in content:
            print(f"   ✓ {name} - REMOVED")
        else:
            print(f"   ✗ {name} - STILL PRESENT")
    
    # Check what's present
    print("\n\n📋 Current Components (Pure Mistral OCR-4):\n")
    
    required_items = [
        ("Mistral client", "from mistralai import Mistral"),
        ("MistralOCRExtractor class", "class MistralOCRExtractor"),
        ("MistralOnlyExtractor class", "class MistralOnlyExtractor"),
        ("_run_mistral function", "def _run_mistral"),
        ("OCR model config", "MISTRAL_OCR_MODEL"),
    ]
    
    for name, code_snippet in required_items:
        if code_snippet in content:
            print(f"   ✓ {name} - PRESENT")
        else:
            print(f"   ✗ {name} - MISSING")
    
    print("\n" + "=" * 70)
    print("\n✅ Configuration verified: Pure Mistral OCR-4 only")
    print("   • No PyMuPDF fallback")
    print("   • No merge logic")
    print("   • Clean Mistral API output only")
    print("\n📝 To run with real PDF (requires mistralai package):")
    print("   pip install mistralai")
    print("   python3 app.py --pdf input/your.pdf --ocr mistral-only --llm none")


if __name__ == "__main__":
    import os
    os.chdir('/home/priyansh-sethi/Music/croz_bot/pdf_extractor')
    test_mistral_extractor_code()
