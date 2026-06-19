"""
gemini_extractor.py
-------------------
Gemini vision provider for the catalog pipeline.
Uses Google Gemini 2.5 Flash (free tier via Google AI Studio).

Drop-in replacement for VisionExtractor — same extract_page() interface.

Setup:
  1. Get a free API key at https://aistudio.google.com
  2. Add to .env:  GEMINI_API_KEY=your_key_here
  3. pip install google-genai
  4. Set provider: gemini  in vision_pipeline/config.yaml
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from vision_pipeline.vision_extractor import VISION_PROMPT, _repair_json


class GeminiExtractor:
    """Extract products from a catalog page PNG using Gemini 2.5 Flash."""

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str, config: dict):
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError(
                "google-genai is required for Gemini provider.\n"
                "Install with: pip install google-genai"
            )

        from google import genai
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.model = config.get("gemini_model", self.DEFAULT_MODEL)
        self.max_tokens = int(config.get("max_tokens", 8192))
        self.max_retries = int(config.get("max_retries", 3))
        self.retry_delay = float(config.get("retry_delay", 2.0))
        self.request_delay = float(config.get("request_delay", 5.0))
        self._last_call_time: float = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call_time
        wait = self.request_delay - elapsed
        if wait > 0:
            time.sleep(wait)

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        """Send one page PNG to Gemini. Returns list of product dicts."""
        from google.genai import types

        self._throttle()
        image_bytes = png_path.read_bytes()

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                        types.Part.from_text(text=VISION_PROMPT),
                    ],
                    config=types.GenerateContentConfig(
                        max_output_tokens=self.max_tokens,
                        temperature=0.0,
                    ),
                )
                self._last_call_time = time.time()
                raw = response.text or ""
                products = _repair_json(raw)
                for p in products:
                    p["page_num"] = page_num
                return products

            except Exception as exc:
                err = str(exc)
                # Re-raise 429s so key_rotator can handle rotation/fallback
                if "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err:
                    self._last_call_time = time.time()
                    raise
                last_error = exc
                err_str = str(exc).lower()
                # Re-raise rate limit AND server overload errors so KeyRotator can rotate keys
                if "429" in err_str or "quota" in err_str or "rate" in err_str or "503" in err_str or "unavailable" in err_str:
                    raise
                print(f"  [page {page_num}] Gemini attempt {attempt}/{self.max_retries} failed: {exc}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)

        self._last_call_time = time.time()
        print(f"  [page {page_num}] All Gemini retries failed: {last_error}")
        return None  # None = hard failure, lets KeyRotator/main distinguish from empty page
