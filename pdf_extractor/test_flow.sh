#!/bin/bash

# Test complete flow with Mistral OCR-4 only + LLM product splitting

cd /home/priyansh-sethi/Music/croz_bot/pdf_extractor

# Install dependencies if needed
# pip install mistralai groq python-dotenv

# Test with a real PDF
python3 app.py --pdf input/BEP-01.pdf --ocr mistral-only --llm none

# Check output
echo ""
echo "Output files:"
ls -lh output/mistral-only/BEP-01/
echo ""
echo "Headings:"
cat output/mistral-only/BEP-01/headings.txt
echo ""
echo "Products:"
cat output/mistral-only/BEP-01/products/manifest.json
