#!/bin/bash

# Test YOLOv8 Layout Detection Extractor

cd /home/priyansh-sethi/Music/croz_bot/pdf_extractor

PYTHON="/home/priyansh-sethi/Music/croz_bot/venv/bin/python3"
PDF="input/Fluid_Handling_m.pdf"
MODEL="best (1).pt"

echo "Testing YOLOv8 Extractor..."
echo "================================"
echo ""
echo "PDF: $PDF"
echo "Model: $MODEL"
echo ""

$PYTHON -c "
import sys
sys.path.insert(0, '.')
from extractors.yolo_extractor import YoloExtractor
import json
from pathlib import Path

print('Extracting layout and headings...')
extractor = YoloExtractor(model_path='$MODEL', confidence=0.5)
result = extractor.extract('$PDF')

if result.success:
    print('✓ Extraction successful\n')
    
    headings = result.json_data.get('headings', [])
    print(f'Found {len(headings)} headings/titles:\n')
    
    for h in headings:
        indent = '  ' * (h['level'] - 1)
        print(f\"{indent}{'#' * h['level']} {h['text']}\")
        print(f\"{indent}   (Page {h['page'] + 1}, confidence: {h['confidence']:.2f})\")
    
    print(f'\nTotal detections: {result.json_data[\"total_detections\"]}')
    
    # Save outputs
    output_dir = Path('output/yolo')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_dir / 'extracted.md', 'w') as f:
        f.write(result.markdown)
    
    with open(output_dir / 'structure.json', 'w') as f:
        json.dump(result.json_data, f, indent=2)
    
    print(f'\nOutputs saved to: {output_dir}/')
    print('  - extracted.md')
    print('  - structure.json')
else:
    print(f'✗ Error: {result.error}')
"

echo ""
echo "================================"
echo "Done! Check output/yolo/ for results"
