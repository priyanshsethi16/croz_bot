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

VISION_PROMPT = """You are an expert industrial product catalog parser with perfect vision and deep knowledge of industrial tools, lubrication equipment, LED lighting, fluid handling systems, and precision instruments.

Analyze this catalog page image and extract EVERY piece of information into structured JSON.
Your goal: capture 100% of visible data so a chatbot can answer ANY question about these products without needing the original PDF.

---

## STEP 1 — READ PAGE STRUCTURE FIRST

Before touching products, identify:
- The SECTION BANNER (large coloured header bar e.g. "GREASE PUMPS & ACCESSORIES", "HAMMERS") — read it EXACTLY, never infer
- How many separate product areas exist — each with its OWN heading block and OWN product image
- Page number if visible

---

## STEP 2 — FAMILY vs CHILD (most important decision)

**Create SEPARATE families (separate array elements) when:**
- Different heading text AND different product image → ALWAYS separate families
- Completely different product function (e.g. ratio pump vs bucket pump on same page) → ALWAYS separate
- Each has its own description block → separate
- When in doubt → separate families

**Create CHILDREN inside one family when:**
- Same heading + multiple coloured code badges (GP0, GP1, GP2...) → ONE family, each badge = one child
- Same product type, different sizes, sharing one description → ONE family, each size = one child
- Multiple codes sharing one ordering table → ONE family, multiple children

---

## STEP 3 — EXTRACTION RULES

**Product codes:**
- Short alphanumeric label in a coloured badge = product code — read EVERY character exactly with hyphens and slashes
- Labels starting with standards prefixes = CERTIFICATIONS, NEVER product codes:
  BS, ISO, DIN, ANSI, ASME, BIS, CE, EN, IEC, NF, JIS, GB, UL, CSA, IP, ATEX

**Specifications — use dedicated fields, never dump into description:**
- Flow rate / output rate → specifications.flow_rate (e.g. "1.1 KG/MIN")
- Pressure values → specifications.pressure
- Motor/cylinder size → specifications.motor_size
- Ratio → specifications.ratio (e.g. "50:1")
- Voltage / battery → specifications.voltage / specifications.battery
- Capacity → specifications.capacity
- IP rating → specifications.ip_rating
- Thread → specifications.thread
- Lumens, runtime → specifications.lumens, specifications.runtime
- Weight at family level → specifications.weight

**Children — NEVER empty:**
- Every family MUST have at least one child
- Single-variant product → one child with same code as family
- All sizes, ordering tables, part numbers → ALWAYS inside children, never at family level

**Capture everything visible:**
- Section banner → page_metadata.catalog_section (exact text, do not infer)
- category field → same as catalog_section text, not a generic invented name
- Badges: NEW, Bestseller, Popular in EUROPE, Patent Pending → notes
- Safety warnings, fine print → safety_warnings
- Warranty, country of origin → dedicated fields
- Any text not fitting schema → raw_text_blocks (nothing dropped silently)
- ★ star on a size row → "bestseller": true on that row
- □ symbol on a row → "made_to_order": true on that row

---

## JSON SCHEMA

Return a JSON array. Each element = one product FAMILY.

[
  {
    "page_metadata": {
      "page_number": "page number if visible, else null",
      "catalog_section": "Exact section banner text e.g. GREASE PUMPS & ACCESSORIES",
      "brand": "Brand name visible e.g. GROZ"
    },
    "product_name": "Exact family name from heading",
    "product_code": "Primary family code — empty string if none",
    "category": "Exact section banner text — same as catalog_section, never invent a category",
    "sub_category": "More specific type e.g. Air Operated Pumps, Ball Peen Hammers",
    "description": "Full shared description — every sentence, do not summarize",
    "features": ["every shared feature bullet — exact wording"],
    "utilities": ["every application/utility bullet"],
    "materials": {
      "head_material": "",
      "handle_material": "",
      "body_material": "",
      "finish": "",
      "other": ""
    },
    "specifications": {
      "standard": "all standards listed e.g. BS 876, DIN 1041",
      "hardness": "",
      "ratio": "",
      "capacity": "",
      "pressure": "",
      "flow_rate": "",
      "motor_size": "",
      "voltage": "",
      "battery": "",
      "lumens": "",
      "runtime": "",
      "ip_rating": "",
      "thread": "",
      "weight": "",
      "operating_temp": "",
      "connection_type": "",
      "other": "any spec not covered above"
    },
    "certifications": [
      {"standard": "BS 876", "description": "what it certifies if explained on page"}
    ],
    "compatibility": {
      "fits_with": [],
      "replacement_parts": [],
      "works_with": ""
    },
    "safety_warnings": [],
    "warranty": "",
    "country_of_origin": "",
    "packaging": {
      "unit_quantity": "",
      "box_quantity": "",
      "packaging_type": ""
    },
    "notes": "Bestseller, NEW, Popular in EUROPE, Patent Pending, footnotes — all combined",
    "raw_text_blocks": ["any visible text that does not fit the fields above"],
    "children": [
      {
        "product_code": "GP0",
        "product_name": "Fits 13.5 kg drums",
        "description": "variant-specific description — empty string if same as family",
        "features": [],
        "materials": {},
        "specifications": {},
        "color": "",
        "sizes": [
          {
            "size": "100 gm",
            "dimensions": "11\" (280 mm)",
            "cat_no": "",
            "ean": "",
            "bestseller": false,
            "new": false,
            "made_to_order": false
          }
        ],
        "ordering_table": [
          {
            "Cat_No": "",
            "Ord_No": "",
            "Size": "",
            "Weight": "",
            "Box_Qty": "",
            "EAN": "",
            "Price": ""
          }
        ],
        "accessories_included": [],
        "notes": ""
      }
    ]
  }
]

---

## FINAL RULES

1. children is NEVER empty — minimum one child per family
2. Capture EVERY size and ordering row — never skip or truncate
3. Read product codes character by character — never guess or abbreviate
4. category = exact section banner text from the page — never invent names like "Lubrication Equipment"
5. Different heading + different image = always separate families, even on same page
6. Specs like flow rate, pressure, ratio belong in specifications fields — not in description
7. Return ONLY the JSON array — no markdown fences, no explanation
8. If page is a cover, TOC, or section divider with no products, return []
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
