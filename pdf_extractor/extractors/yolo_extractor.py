"""
yolo_extractor.py
-----------------
YOLOv8-based PDF layout detection and extraction.
Uses a trained YOLOv8 model to detect document elements:
- Delete, Formula, Image, Paragraph, Paragraph_Title, 
  Section_Title, Special_Paragraph, Table

Focuses on extracting headings and titles with proper hierarchy.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from PIL import Image

from .base import BaseExtractor, ExtractionResult


class YoloExtractor(BaseExtractor):
    """Extract PDF layout using YOLOv8 trained model."""

    def __init__(self, model_path: str = "best (1).pt", confidence: float = 0.5):
        """
        Initialize YOLO extractor.
        
        Args:
            model_path: Path to trained YOLOv8 model weights
            confidence: Confidence threshold for detections (0.0-1.0)
        """
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError(
                "ultralytics is required for YOLO extraction.\n"
                "Install with: pip install ultralytics"
            )
        
        # Handle relative and absolute paths
        model_path = Path(model_path)
        if not model_path.is_absolute():
            # Try current directory first
            if not model_path.exists():
                # Try extractors directory
                extractor_model = Path(__file__).parent / model_path.name
                if extractor_model.exists():
                    model_path = extractor_model
                # Try pdf_extractor root
                elif (Path(__file__).parent.parent / model_path.name).exists():
                    model_path = Path(__file__).parent.parent / model_path.name
        
        if not model_path.exists():
            raise FileNotFoundError(
                f"YOLO model not found: {model_path}\n"
                f"Searched in: current dir, extractors/, pdf_extractor/"
            )
        
        self.model_path = model_path
        self.model = YOLO(str(self.model_path))
        self.confidence = confidence
        
        # Class mapping from your trained model
        self.classes = {
            0: "Delete",
            1: "Formula",
            2: "Image",
            3: "Paragraph",
            4: "Paragraph_Title",
            5: "Section_Title",
            6: "Special_Paragraph",
            7: "Table",
        }

    def extract(self, pdf_path: str) -> ExtractionResult:
        """Extract layout and content from PDF using YOLO detection."""
        try:
            pdf_path = Path(pdf_path)
            if not pdf_path.exists():
                return ExtractionResult(
                    engine="yolo",
                    success=False,
                    error=f"PDF not found: {pdf_path}"
                )
            
            doc = fitz.open(pdf_path)
            all_detections = []
            markdown_parts = []
            json_structure = []
            
            for page_num in range(len(doc)):
                page = doc[page_num]
                detections = self._detect_page_layout(page, page_num)
                all_detections.extend(detections)
                
                # Sort detections by vertical position (top to bottom)
                page_detections = sorted(
                    [d for d in detections if d["page"] == page_num],
                    key=lambda x: x["bbox"][1]  # Sort by y_min
                )
                
                # Extract text for each detection
                for det in page_detections:
                    text = self._extract_text_from_bbox(page, det["bbox"])
                    det["text"] = text
                    
                    # Build markdown
                    md = self._format_as_markdown(det)
                    if md:
                        markdown_parts.append(md)
                    
                    # Build JSON structure
                    json_structure.append({
                        "page": det["page"],
                        "type": det["class_name"],
                        "confidence": det["confidence"],
                        "bbox": det["bbox"],
                        "text": text,
                    })
            
            doc.close()
            
            # Extract headings and titles
            headings = self._extract_headings(json_structure)
            
            markdown = "\n\n".join(markdown_parts)
            
            return ExtractionResult(
                engine="yolo",
                markdown=markdown,
                text=self._markdown_to_text(markdown),
                json_data={
                    "detections": json_structure,
                    "headings": headings,
                    "total_detections": len(all_detections),
                },
                success=True,
            )
        
        except Exception as e:
            return ExtractionResult(
                engine="yolo",
                success=False,
                error=f"YOLO extraction failed: {str(e)}"
            )

    def _detect_page_layout(self, page: fitz.Page, page_num: int) -> list[dict]:
        """Run YOLO detection on a single PDF page."""
        # Convert page to image
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        
        # Run YOLO detection
        results = self.model(img, conf=self.confidence, verbose=False)
        
        detections = []
        for result in results:
            boxes = result.boxes
            for box in boxes:
                cls_id = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                
                # Scale coordinates to PDF space
                scale_x = page.rect.width / pix.width
                scale_y = page.rect.height / pix.height
                
                bbox = [
                    x1 * scale_x,
                    y1 * scale_y,
                    x2 * scale_x,
                    y2 * scale_y,
                ]
                
                detections.append({
                    "page": page_num,
                    "class_id": cls_id,
                    "class_name": self.classes.get(cls_id, f"Unknown_{cls_id}"),
                    "confidence": conf,
                    "bbox": bbox,
                })
        
        return detections

    def _extract_text_from_bbox(self, page: fitz.Page, bbox: list[float]) -> str:
        """Extract text from a specific bounding box on the page."""
        rect = fitz.Rect(bbox)
        text = page.get_text("text", clip=rect).strip()
        return text

    def _format_as_markdown(self, detection: dict) -> str:
        """Format detection as markdown based on its type."""
        text = detection.get("text", "").strip()
        if not text:
            return ""
        
        class_name = detection["class_name"]
        
        if class_name == "Section_Title":
            return f"# {text}"
        elif class_name == "Paragraph_Title":
            return f"## {text}"
        elif class_name == "Special_Paragraph":
            return f"**{text}**"
        elif class_name == "Paragraph":
            return text
        elif class_name == "Table":
            return f"```\n{text}\n```"
        elif class_name == "Formula":
            return f"$$\n{text}\n$$"
        elif class_name == "Image":
            return f"![Image on page {detection['page'] + 1}]"
        elif class_name == "Delete":
            return ""  # Skip deleted content
        
        return text

    def _extract_headings(self, detections: list[dict]) -> list[dict]:
        """Extract hierarchical headings from detections."""
        headings = []
        
        for det in detections:
            class_name = det["type"]
            if class_name in ["Section_Title", "Paragraph_Title"]:
                headings.append({
                    "page": det["page"],
                    "level": 1 if class_name == "Section_Title" else 2,
                    "text": det["text"],
                    "type": class_name,
                    "confidence": det["confidence"],
                    "bbox": det["bbox"],
                })
        
        return headings

    def _markdown_to_text(self, markdown: str) -> str:
        """Convert markdown to plain text."""
        # Remove markdown formatting
        text = markdown.replace("#", "")
        text = text.replace("**", "")
        text = text.replace("```", "")
        text = text.replace("$$", "")
        return text.strip()

    def extract_headings_only(self, pdf_path: str) -> dict[str, Any]:
        """
        Extract only headings and titles from PDF.
        
        Returns:
            Dictionary with headings hierarchy
        """
        result = self.extract(pdf_path)
        if not result.success:
            return {"error": result.error, "headings": []}
        
        return result.json_data.get("headings", [])

    def visualize_detections(self, pdf_path: str, output_dir: str = "output/yolo_viz"):
        """
        Create visualization of YOLO detections on PDF pages.
        
        Args:
            pdf_path: Path to input PDF
            output_dir: Directory to save visualizations
        """
        try:
            from ultralytics import YOLO
            import cv2
            import numpy as np
        except ImportError:
            print("OpenCV is required for visualization. Install: pip install opencv-python")
            return
        
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        doc = fitz.open(pdf_path)
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            pix = page.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            
            # Run detection
            results = self.model(img, conf=self.confidence)
            
            # Save annotated image
            for i, result in enumerate(results):
                annotated = result.plot()
                output_file = output_path / f"page_{page_num + 1:03d}.png"
                cv2.imwrite(str(output_file), annotated)
        
        doc.close()
        print(f"Visualizations saved to: {output_path}")


def test_yolo_extractor():
    """Test the YOLO extractor."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python yolo_extractor.py <pdf_path> [model_path]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    model_path = sys.argv[2] if len(sys.argv) > 2 else "best (1).pt"
    
    extractor = YoloExtractor(model_path=model_path, confidence=0.5)
    
    print(f"Extracting from: {pdf_path}")
    print(f"Using model: {model_path}")
    print("-" * 60)
    
    result = extractor.extract(pdf_path)
    
    if result.success:
        print("✓ Extraction successful\n")
        
        headings = result.json_data.get("headings", [])
        print(f"Found {len(headings)} headings/titles:\n")
        
        for heading in headings:
            indent = "  " * (heading["level"] - 1)
            print(f"{indent}{'#' * heading['level']} {heading['text']}")
            print(f"{indent}   (Page {heading['page'] + 1}, confidence: {heading['confidence']:.2f})")
        
        print(f"\n\nTotal detections: {result.json_data['total_detections']}")
        
        # Save outputs
        output_dir = Path("output/yolo")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(output_dir / "extracted.md", "w") as f:
            f.write(result.markdown)
        
        with open(output_dir / "structure.json", "w") as f:
            json.dump(result.json_data, f, indent=2)
        
        print(f"\nOutputs saved to: {output_dir}/")
        
        # Create visualizations
        print("\nCreating visualizations...")
        extractor.visualize_detections(pdf_path)
    else:
        print(f"✗ Extraction failed: {result.error}")


if __name__ == "__main__":
    test_yolo_extractor()
