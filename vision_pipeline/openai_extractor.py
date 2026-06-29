"""
openai_extractor.py
-------------------
OpenAI Vision provider for the catalog pipeline.
Supports GPT-4 Vision and GPT-5 series models.

Drop-in replacement for VisionExtractor — same extract_page() interface.

Setup:
  1. Get API key from https://platform.openai.com/api-keys
  2. Add to .env:  OPENAI_API_KEY=your_key_here
  3. pip install openai
  4. Set provider: openai  in vision_pipeline/config.yaml
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from vision_pipeline.vision_extractor import build_vision_prompt, _repair_json


class OpenAIExtractor:
    """Extract products from a catalog page PNG using OpenAI GPT-4/5 Vision."""

    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, api_key: str, config: dict):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai is required for OpenAI provider.\n"
                "Install with: pip install openai"
            )

        self.model = config.get("openai_model") or self.DEFAULT_MODEL
        self.prompt = build_vision_prompt(config)
        self.client = OpenAI(api_key=api_key)
        self.temperature = float(config.get("temperature", 0.0))
        self.max_tokens = int(config.get("max_tokens", 16384))
        self.max_retries = int(config.get("max_retries", 3))
        self.retry_delay = float(config.get("retry_delay", 2.0))
        self.request_delay = float(config.get("request_delay", 1.0))
        self._last_call_time: float = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_call_time
        wait = self.request_delay - elapsed
        if wait > 0:
            time.sleep(wait)

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        """Send one page PNG to OpenAI Vision. Returns list of product dicts."""
        self._throttle()
        image_bytes = png_path.read_bytes()
        image_b64 = base64.b64encode(image_bytes).decode("ascii")

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": self.prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{image_b64}"
                                    },
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
                err = str(exc)
                # Re-raise rate limit errors so key_rotator can handle rotation
                if "429" in err or "rate_limit" in err.lower() or "quota" in err.lower():
                    self._last_call_time = time.time()
                    raise
                last_error = exc
                err_str = str(exc).lower()
                # Re-raise server errors for key rotation
                if "429" in err_str or "quota" in err_str or "rate" in err_str or "503" in err_str or "unavailable" in err_str:
                    raise
                print(f"  [page {page_num}] OpenAI attempt {attempt}/{self.max_retries} failed: {exc}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)

        self._last_call_time = time.time()
        print(f"  [page {page_num}] All OpenAI retries failed: {last_error}")
        return None  # None = hard failure
