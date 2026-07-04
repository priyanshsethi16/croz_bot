import os
import json
from typing import List, Dict, Optional

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False
    Groq = None


class ProductIdentifier:
    """Uses LLM to identify actual Groz products from headings and split content."""
    
    def __init__(self, llm_provider: str = "groq"):
        if not GROQ_AVAILABLE:
            raise ImportError("groq package not installed. Run: pip install groq")
        
        self.llm_provider = llm_provider
        if llm_provider == "groq":
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise ValueError("GROQ_API_KEY missing in .env")
            self.client = Groq(api_key=api_key)
            self.model = os.getenv("GROQ_MODEL", "qwen/qwen-2.5-72b-instruct")
    
    def identify_products(self, headings: List[Dict[str, str]]) -> List[Dict]:
        """
        Send headings to LLM to identify actual Groz products.
        
        Returns list of identified products with start/end line numbers.
        """
        headings_text = self._format_headings_for_llm(headings)
        
        prompt = f"""You are analyzing a Groz industrial catalog. Given the following headings extracted from the document, identify which headings represent actual Groz products (tools, equipment, hardware items).

RULES:
- Include only actual product names (e.g., "Cordless Drill", "Club Hammer", "Screwdriver Set")
- Exclude generic sections like "Features", "Specifications", "Contents", "Index", "Introduction"
- Exclude sub-sections that are part of product description (Features, Technical Details, etc.)
- Each product should be a standalone tool/equipment
- Products may span multiple pages

HEADINGS:
{headings_text}

Return ONLY a JSON array of products with this exact format:
[
  {{
    "product_name": "Cordless Drill",
    "start_line": 10,
    "end_line": 45,
    "heading_level": 1
  }}
]

Return ONLY the JSON array, no other text."""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a product catalog analyzer. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )
        
        result = response.choices[0].message.content.strip()
        # Strip thinking blocks (qwen3 thinking mode)
        import re as _re
        result = _re.sub(r'<think>.*?</think>', '', result, flags=_re.DOTALL).strip()
        # Remove markdown code blocks if present
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        
        products = json.loads(result)
        return products
    
    def split_text_by_products(
        self, 
        full_text: str, 
        products: List[Dict]
    ) -> List[Dict]:
        """
        Split OCR text into chunks based on identified product boundaries.
        
        Returns list of product chunks with text content.
        """
        lines = full_text.split('\n')
        chunks = []
        
        for i, product in enumerate(products):
            start_line = product['start_line'] - 1  # Convert to 0-indexed
            
            # Determine end line
            if i < len(products) - 1:
                end_line = products[i + 1]['start_line'] - 1
            else:
                end_line = len(lines)
            
            # Extract text chunk
            chunk_lines = lines[start_line:end_line]
            chunk_text = '\n'.join(chunk_lines).strip()
            
            chunks.append({
                'product_name': product['product_name'],
                'start_line': product['start_line'],
                'end_line': end_line + 1,  # Convert back to 1-indexed
                'text': chunk_text,
                'char_count': len(chunk_text)
            })
        
        return chunks
    
    def _format_headings_for_llm(self, headings: List[Dict[str, str]]) -> str:
        """Format headings list for LLM prompt."""
        lines = []
        for h in headings:
            indent = '  ' * (h['level'] - 1)
            lines.append(f"Line {h['line_number']:4d}: {indent}{'#' * h['level']} {h['text']}")
        return '\n'.join(lines)
    
    def save_chunks(self, chunks: List[Dict], output_dir: str):
        """Save product chunks as separate markdown files."""
        os.makedirs(f"{output_dir}/products", exist_ok=True)
        
        # Save manifest
        manifest_path = f"{output_dir}/products/manifest.json"
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump([{k: v for k, v in c.items() if k != 'text'} for c in chunks], 
                     f, indent=2, ensure_ascii=False)
        
        # Save individual product markdown files
        for i, chunk in enumerate(chunks, 1):
            slug = chunk['product_name'].lower().replace(' ', '_').replace('/', '_')
            slug = ''.join(c for c in slug if c.isalnum() or c == '_')
            
            md_path = f"{output_dir}/products/{i:04d}_{slug}.md"
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(f"# {chunk['product_name']}\n\n")
                f.write(f"**Lines:** {chunk['start_line']}-{chunk['end_line']}\n\n")
                f.write("---\n\n")
                f.write(chunk['text'])
        
        return manifest_path
    
    def process_and_split(
        self,
        headings: List[Dict[str, str]],
        full_text: str,
        output_dir: str,
        print_fn=print,
    ) -> tuple[List[Dict], str]:
        """
        Complete workflow: identify products and split text.
        
        Returns (chunks, manifest_path)
        """
        print_fn("🤖 Identifying Groz products from headings...")
        products = self.identify_products(headings)
        print_fn(f"   Found {len(products)} Groz product(s)")
        
        print_fn("✂️  Splitting text by products...")
        chunks = self.split_text_by_products(full_text, products)
        
        print_fn("💾 Saving product chunks...")
        manifest_path = self.save_chunks(chunks, output_dir)
        
        return chunks, manifest_path
