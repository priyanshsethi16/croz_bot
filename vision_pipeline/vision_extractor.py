"""
vision_extractor.py
-------------------
Sends a single catalog page PNG to Llama 4 Scout (Groq) and returns
a list of structured product dicts extracted directly from the image.

No OCR step — the vision model reads layout, columns, and tables visually.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from groq import Groq


# ── Prompt ───────────────────────────────────────────────────────────────────

VISION_PROMPT = """Expert industrial catalog parser. Extract ALL product data into JSON.

## STEP 1 — PAGE STRUCTURE
Identify: section banner (exact text), number of distinct product headings, page number.

## STEP 2 — FAMILY vs CHILD
NEW HEADING = NEW FAMILY. Scan full page, count headings. Each heading = one array element.

Separate families when: different heading text, different product image, different function.
Children within one family when: same heading + multiple code badges, same product in different sizes sharing one description, multiple codes under one heading, ordering table with multiple part numbers.

## STEP 3 — RULES

**Product codes:** Alphanumeric label in coloured badge. Read every character exactly.
NOT codes: BS, ISO, DIN, ANSI, ASME, BIS, CE, EN, IEC, NF, JIS, GB, UL, CSA, IP, ATEX — these are certifications.

**Specs — use dedicated fields:**
flow_rate, pressure, motor_size, ratio, voltage, battery, capacity, ip_rating, thread, lumens, runtime, weight, operating_temp, connection_type. Never dump specs into description.

**Children — NEVER empty:**
- Min 1 child per family. Single product → child with same code as family.
- Sizes/ordering tables → always in children, never at family level.
- Text under badge → child's product_name (e.g. "750cc High capacity", "Popular in Australia & NZ").
- SIZES list with no per-row codes → one child per size row, size = product_name, family code = product_code.
- Shared features/description → family level only, don't repeat in children.

**Capture everything:**
- Section banner → page_metadata.catalog_section (exact, never infer)
- category = catalog_section text exactly
- NEW, Bestseller, Popular in EUROPE, Patent Pending → notes
- ★ on row → "bestseller": true; □ on row → "made_to_order": true
- Safety warnings, warranty, country of origin → dedicated fields
- Unclassified text → raw_text_blocks

## JSON SCHEMA
Return JSON array only. Each element = one product family.

[{
  "page_metadata": {"page_number": null, "catalog_section": "", "brand": ""},
  "product_name": "",
  "product_code": "",
  "category": "",
  "sub_category": "",
  "description": "",
  "features": [],
  "utilities": [],
  "materials": {"head_material": "", "handle_material": "", "body_material": "", "finish": "", "other": ""},
  "specifications": {"standard": "", "hardness": "", "ratio": "", "capacity": "", "pressure": "", "flow_rate": "", "motor_size": "", "voltage": "", "battery": "", "lumens": "", "runtime": "", "ip_rating": "", "thread": "", "weight": "", "operating_temp": "", "connection_type": "", "other": ""},
  "certifications": [{"standard": "", "description": ""}],
  "compatibility": {"fits_with": [], "replacement_parts": [], "works_with": ""},
  "safety_warnings": [],
  "warranty": "",
  "country_of_origin": "",
  "packaging": {"unit_quantity": "", "box_quantity": "", "packaging_type": ""},
  "notes": "",
  "raw_text_blocks": [],
  "children": [{
    "product_code": "",
    "product_name": "",
    "description": "",
    "features": [],
    "materials": {},
    "specifications": {},
    "color": "",
    "sizes": [{"size": "", "dimensions": "", "cat_no": "", "ean": "", "bestseller": false, "new": false, "made_to_order": false}],
    "ordering_table": [{"Cat_No": "", "Ord_No": "", "Size": "", "Weight": "", "Box_Qty": "", "EAN": "", "Price": ""}],
    "accessories_included": [],
    "notes": ""
  }]
}]

## FINAL RULES
1. children NEVER empty — min 1 child per family
2. Capture EVERY size and ordering row
3. Read product codes character by character
4. category = exact section banner text
5. Different heading + image = separate families
6. Return ONLY the JSON array — no markdown, no explanation
7. Cover/TOC/divider page with no products → return []
"""


# ── JSON repair ───────────────────────────────────────────────────────────────

def _repair_json(text: str) -> list[dict]:
    """Extract and parse JSON array from LLM response, with light repair."""
    # Strip thinking blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "")
    # Find outermost array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    candidate = text[start : end + 1]
    # Remove trailing commas
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        result = json.loads(candidate)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        candidate = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)
        try:
            result = json.loads(candidate)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            return []


# ── Main extractor class ──────────────────────────────────────────────────────

class VisionExtractor:
    """Extract products from a catalog page PNG using Llama Vision on Groq."""

    def __init__(self, groq_api_key: str, config: dict):
        self.client = Groq(api_key=groq_api_key)
        self.model = config.get("vision_model", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.temperature = float(config.get("temperature", 0.1))
        self.max_tokens = int(config.get("max_tokens", 8192))
        self.max_retries = int(config.get("max_retries", 3))
        self.retry_delay = float(config.get("retry_delay", 2.0))
        self.request_delay = float(config.get("request_delay", 5.0))
        self._last_call_time: float = 0.0

    def _encode_image(self, png_path: Path) -> str:
        """Base64-encode a PNG file."""
        return base64.b64encode(png_path.read_bytes()).decode("utf-8")

    def _throttle(self):
        """Ensure minimum gap between API calls to respect rate limits."""
        elapsed = time.time() - self._last_call_time
        wait = self.request_delay - elapsed
        if wait > 0:
            time.sleep(wait)

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        """
        Send one page PNG to Llama Vision. Returns list of product dicts.
        Empty list if page has no products (cover, TOC, etc.).
        """
        self._throttle()
        b64 = self._encode_image(png_path)

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{b64}",
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": VISION_PROMPT,
                                },
                            ],
                        }
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                self._last_call_time = time.time()
                raw = response.choices[0].message.content or ""
                products = _repair_json(raw)
                for p in products:
                    p["page_num"] = page_num
                return products

            except Exception as exc:
                last_error = exc
                print(f"  [page {page_num}] Vision attempt {attempt}/{self.max_retries} failed: {exc}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)

        self._last_call_time = time.time()
        print(f"  [page {page_num}] All retries failed: {last_error}")
        return []
