import re
from typing import List, Dict
from extractors.base import ExtractionResult


class HeadingExtractor:
    """Extracts headings and subheadings from OCR markdown text."""
    
    def extract_headings(self, text: str) -> List[Dict[str, str]]:
        """
        Extract all markdown headings from text.
        
        Returns:
            List of dicts with 'level', 'text', and 'line_number'
        """
        headings = []
        lines = text.split('\n')
        
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            
            # Match markdown headings (# Heading)
            match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if match:
                level = len(match.group(1))
                heading_text = match.group(2).strip()
                headings.append({
                    'level': level,
                    'text': heading_text,
                    'line_number': line_num
                })
        
        return headings
    
    def format_headings(self, headings: List[Dict[str, str]]) -> str:
        """Format headings as indented text."""
        lines = []
        for h in headings:
            indent = '  ' * (h['level'] - 1)
            lines.append(f"{indent}{'#' * h['level']} {h['text']} (line {h['line_number']})")
        return '\n'.join(lines)
    
    def save_headings(self, headings: List[Dict[str, str]], output_path: str):
        """Save headings to a text file."""
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(self.format_headings(headings))
    
    def extract_and_save(self, result: ExtractionResult, output_path: str) -> List[Dict[str, str]]:
        """Extract headings from ExtractionResult and save to file."""
        headings = self.extract_headings(result.markdown)
        self.save_headings(headings, output_path)
        return headings
