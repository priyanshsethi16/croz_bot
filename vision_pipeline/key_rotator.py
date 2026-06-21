"""Gemini key rotation for the admin-selected LangChain vision model."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class KeyRotator:
    """Rotate configured Gemini keys while preserving one selected VLM model."""

    def __init__(self, cfg: dict):
        self._gemini_cfg = dict(cfg.get("gemini", {}))
        if cfg.get("parsing_instructions"):
            self._gemini_cfg["parsing_instructions"] = cfg["parsing_instructions"]
        self._model = (
            os.getenv("GEMINI_VISION_MODEL", "").strip()
            or self._gemini_cfg.get("gemini_model")
            or "gemini-2.5-flash"
        )
        self._gemini_cfg["gemini_model"] = self._model
        self._keys: list[str] = []
        for name in ["GEMINI_API_KEY", *[f"GEMINI_API_KEY_{i}" for i in range(1, 20)]]:
            key = os.getenv(name, "").strip()
            if key and key not in self._keys:
                self._keys.append(key)
        if not self._keys:
            raise ValueError("GEMINI_API_KEY is required for PDF chunk extraction.")
        self._active_idx = 0

    def _extractor(self, key: str):
        from vision_pipeline.gemini_extractor import GeminiExtractor
        return GeminiExtractor(key, self._gemini_cfg)

    def describe(self) -> str:
        return f"Gemini / {self._model} via LangChain — {len(self._keys)} key(s)"

    def active_provider(self) -> str:
        return f"{self._model}[key{self._active_idx + 1}]"

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]] | None:
        last_error: Exception | None = None
        for offset in range(len(self._keys)):
            idx = (self._active_idx + offset) % len(self._keys)
            try:
                result = self._extractor(self._keys[idx]).extract_page(png_path, page_num)
                self._active_idx = idx
                return result
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if "429" in message or "quota" in message or "resource_exhausted" in message:
                    print(f"  [page {page_num}] Gemini key {idx + 1} quota exhausted; rotating")
                    continue
                raise
        print(f"  [page {page_num}] All Gemini keys failed: {last_error}")
        return None
