"""Multi-provider key rotation for vision models (Gemini, OpenAI, Groq)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class KeyRotator:
    """Rotate API keys across providers while preserving selected VLM model."""

    def __init__(self, cfg: dict):
        self.provider = cfg.get("provider", "gemini").lower()
        
        if self.provider == "openai":
            self._init_openai(cfg)
        elif self.provider == "groq":
            self._init_groq(cfg)
        else:
            self._init_gemini(cfg)

    def _init_openai(self, cfg: dict):
        self._cfg = dict(cfg.get("openai", {}))
        if cfg.get("parsing_instructions"):
            self._cfg["parsing_instructions"] = cfg["parsing_instructions"]
        self._model = (
            os.getenv("OPENAI_VISION_MODEL", "").strip()
            or self._cfg.get("openai_model")
            or "gpt-4o"
        )
        self._cfg["openai_model"] = self._model
        self._keys: list[str] = []
        for name in ["OPENAI_API_KEY", *[f"OPENAI_API_KEY_{i}" for i in range(1, 20)]]:
            key = os.getenv(name, "").strip()
            if key and key not in self._keys:
                self._keys.append(key)
        if not self._keys:
            raise ValueError("OPENAI_API_KEY is required for OpenAI provider.")
        self._active_idx = 0

    def _init_groq(self, cfg: dict):
        self._cfg = dict(cfg.get("groq", {}))
        if cfg.get("parsing_instructions"):
            self._cfg["parsing_instructions"] = cfg["parsing_instructions"]
        self._model = (
            os.getenv("GROQ_VISION_MODEL", "").strip()
            or self._cfg.get("vision_model")
            or "meta-llama/llama-4-scout-17b-16e-instruct"
        )
        self._cfg["vision_model"] = self._model
        self._keys: list[str] = []
        for name in ["GROQ_API_KEY", *[f"GROQ_API_KEY_{i}" for i in range(1, 20)]]:
            key = os.getenv(name, "").strip()
            if key and key not in self._keys:
                self._keys.append(key)
        if not self._keys:
            raise ValueError("GROQ_API_KEY is required for Groq provider.")
        self._active_idx = 0

    def _init_gemini(self, cfg: dict):
        self._cfg = dict(cfg.get("gemini", {}))
        if cfg.get("parsing_instructions"):
            self._cfg["parsing_instructions"] = cfg["parsing_instructions"]
        self._model = (
            os.getenv("GEMINI_VISION_MODEL", "").strip()
            or self._cfg.get("gemini_model")
            or "gemini-2.5-flash"
        )
        self._cfg["gemini_model"] = self._model
        self._keys: list[str] = []
        for name in ["GEMINI_API_KEY", *[f"GEMINI_API_KEY_{i}" for i in range(1, 20)]]:
            key = os.getenv(name, "").strip()
            if key and key not in self._keys:
                self._keys.append(key)
        if not self._keys:
            raise ValueError("GEMINI_API_KEY is required for Gemini provider.")
        self._active_idx = 0

    def _extractor(self, key: str):
        if self.provider == "openai":
            from vision_pipeline.openai_extractor import OpenAIExtractor
            return OpenAIExtractor(key, self._cfg)
        elif self.provider == "groq":
            from vision_pipeline.vision_extractor import VisionExtractor
            return VisionExtractor(key, self._cfg)
        else:
            from vision_pipeline.gemini_extractor import GeminiExtractor
            return GeminiExtractor(key, self._cfg)

    def describe(self) -> str:
        if self.provider == "openai":
            return f"OpenAI / {self._model} — {len(self._keys)} key(s)"
        elif self.provider == "groq":
            return f"Groq / {self._model} — {len(self._keys)} key(s)"
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
                if "429" in message or "quota" in message or "resource_exhausted" in message or "rate" in message:
                    print(f"  [page {page_num}] {self.provider.title()} key {idx + 1} quota exhausted; rotating")
                    continue
                raise
        print(f"  [page {page_num}] All {self.provider.title()} keys failed: {last_error}")
        return None
