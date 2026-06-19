"""
key_rotator.py
--------------
Manages multiple Gemini API keys with automatic rotation on rate limit.
Falls back to Groq (Llama 4 Scout) when all Gemini keys are exhausted.

.env format:
    GEMINI_API_KEY_1=...
    GEMINI_API_KEY_2=...
    GEMINI_API_KEY_3=...
    GEMINI_API_KEY_4=...
    GROQ_API_KEY=...        ← fallback

config.yaml:
    provider: "gemini"      ← or "groq" to skip Gemini entirely
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class KeyRotator:
    """
    Wraps multiple Gemini extractors + one Groq fallback.
    Rotates Gemini keys on 429 (daily limit). Falls back to Groq
    when all Gemini keys are exhausted.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._provider = cfg.get("provider", "groq").lower()
        self._extractors: list = []        # Gemini extractors, one per key
        self._active_idx: int = 0          # current Gemini key index
        self._groq_extractor = None        # Groq fallback
        self._using_fallback: bool = False

        if self._provider == "gemini":
            self._build_gemini_extractors()

        # Always build Groq as fallback (or primary if provider=groq)
        self._build_groq_extractor()

    # ── Builder helpers ───────────────────────────────────────────────────

    def _build_gemini_extractors(self):
        from vision_pipeline.gemini_extractor import GeminiExtractor
        gemini_cfg = self._cfg.get("gemini", {})

        # Collect keys: GEMINI_API_KEY_1 ... GEMINI_API_KEY_N, then GEMINI_API_KEY
        keys: list[str] = []
        for i in range(1, 10):
            k = os.getenv(f"GEMINI_API_KEY_{i}", "")
            if k:
                keys.append(k)
        # Also accept plain GEMINI_API_KEY as key 1
        plain = os.getenv("GEMINI_API_KEY", "")
        if plain and plain not in keys:
            keys.insert(0, plain)

        if not keys:
            print("WARNING: No GEMINI_API_KEY found in .env — falling back to Groq")
            self._provider = "groq"
            return

        for key in keys:
            self._extractors.append(GeminiExtractor(key, gemini_cfg))

        print(f"Gemini keys loaded: {len(keys)}")

    def _build_groq_extractor(self):
        from vision_pipeline.vision_extractor import VisionExtractor
        groq_key = os.getenv("GROQ_API_KEY", "")
        if groq_key:
            self._groq_extractor = VisionExtractor(groq_key, self._cfg.get("groq", {}))

    # ── Public interface ──────────────────────────────────────────────────

    def describe(self) -> str:
        if self._provider != "gemini" or not self._extractors:
            return f"Groq ({self._cfg.get('groq', {}).get('vision_model', 'llama-4-scout')})"
        model = self._cfg.get("gemini", {}).get("gemini_model", "gemini-2.5-flash")
        return f"Gemini ({model}) — {len(self._extractors)} key(s) + Groq fallback"

    def active_provider(self) -> str:
        if self._using_fallback or self._provider != "gemini":
            return "groq"
        return f"gemini-{self._active_idx + 1}"

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        """
        Extract products from one page PNG.
        Rotates Gemini keys on rate limit; falls back to Groq if all exhausted.
        """
        if self._provider != "gemini" or not self._extractors or self._using_fallback:
            return self._groq_extract(png_path, page_num)

        # Try Gemini keys in order
        while self._active_idx < len(self._extractors):
            extractor = self._extractors[self._active_idx]
            try:
                return extractor.extract_page(png_path, page_num)
            except Exception as exc:
                err = str(exc)
                is_quota = "429" in err or "quota" in err.lower() or "RESOURCE_EXHAUSTED" in err
                # Daily project-level limit — rotating keys won't help, go straight to Groq
                is_daily = "PerDay" in err or "per_day" in err.lower()

                if is_quota:
                    if is_daily:
                        print(f"\n  Gemini daily project quota hit — falling back to Groq")
                        self._using_fallback = True
                        return self._groq_extract(png_path, page_num)
                    # Per-minute rate limit — rotate key
                    self._active_idx += 1
                    if self._active_idx < len(self._extractors):
                        print(f"\n  Gemini key {self._active_idx} rate limited — rotating to key {self._active_idx + 1}")
                    else:
                        print("\n  All Gemini keys exhausted — falling back to Groq")
                        self._using_fallback = True
                        return self._groq_extract(png_path, page_num)
                else:
                    print(f"  [page {page_num}] Gemini error: {exc}")
                    return []

        # All keys tried
        self._using_fallback = True
        return self._groq_extract(png_path, page_num)

    def _groq_extract(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        if self._groq_extractor is None:
            print(f"  [page {page_num}] No Groq fallback available")
            return []
        return self._groq_extractor.extract_page(png_path, page_num)
