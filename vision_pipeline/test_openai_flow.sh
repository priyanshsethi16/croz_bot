#!/bin/bash

# Test OpenAI Vision Pipeline
# Usage: ./test_openai_flow.sh

cd /home/priyansh-sethi/Music/croz_bot

# Test with OpenAI GPT-4o
echo "Testing OpenAI Vision Pipeline with GPT-4o..."
python -m vision_pipeline.main --pdf input/BEP-01.pdf --pages 1 3

echo ""
echo "✓ Test complete!"
echo "Check vision_pipeline/data/ for output"
