"""
key_rotator.py
--------------
Multi-tier Gemini key rotation with Groq fallback.

Tier order (best quality first):
  1. gemini-3.5-flash     — all 5 keys (1500 req/day each)
  2. gemini-2.5-flash     — all 5 keys (250 req/day each)
  3. gemini-2.5-flash-lite — all 5 keys (1000 req/day each)
  4. Groq (Llama 4 Scout) — final fallback

On 429 (quota) → key is dead for the day, rotate to next key in same tier.
On 503 (overload) → rotate to next key in same tier (different server).
When all keys in a tier are exhausted/overloaded → drop to next tier.
When Gemini returns empty on a large page → try Groq as sanity check.

.env format:
    GEMINI_API_KEY_1=...  (up to GEMINI_API_KEY_9)
    GROQ_API_KEY=...

config.yaml:
    provider: "gemini"    ← or "groq" to skip Gemini entirely
    gemini:
      gemini_model: "gemini-3.5-flash"   ← starting tier model
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# Model tiers in priority order — first working tier wins
GEMINI_TIERS = [
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]


class KeyRotator:
    """
    Multi-tier Gemini extractor with per-tier key rotation and Groq fallback.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._provider = cfg.get("provider", "groq").lower()
        self._gemini_cfg = cfg.get("gemini", {})
        self._groq_extractor = None

        # Keys shared across all tiers
        self._keys: list[str] = []

        # Per-tier state: {model: {"active_idx": int, "exhausted_keys": set}}
        self._tier_state: dict[str, dict] = {}

        # Current tier index into GEMINI_TIERS
        self._tier_idx: int = 0

        # Whether we've fallen back to Groq entirely
        self._using_groq: bool = False

        if self._provider == "gemini":
            self._load_keys()
            self._init_tiers()

        self._build_groq_extractor()

    # ── Setup ─────────────────────────────────────────────────────────────

    def _load_keys(self):
        for i in range(1, 20):  # support up to 19 keys
            k = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
            if k:
                self._keys.append(k)
        plain = os.getenv("GEMINI_API_KEY", "").strip()
        if plain and plain not in self._keys:
            self._keys.insert(0, plain)

        if not self._keys:
            print("WARNING: No GEMINI_API_KEY found in .env — falling back to Groq")
            self._provider = "groq"
            return

        print(f"Gemini keys loaded: {len(self._keys)}")

    def _init_tiers(self):
        for model in GEMINI_TIERS:
            self._tier_state[model] = {
                "active_idx": 0,
                "exhausted_keys": set(),   # permanently dead (429)
                "overloaded_keys": set(),  # temporarily overloaded (503)
            }

    def _build_groq_extractor(self):
        from vision_pipeline.vision_extractor import VisionExtractor
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        if groq_key:
            self._groq_extractor = VisionExtractor(groq_key, self._cfg.get("groq", {}))

    def _make_extractor(self, key: str, model: str):
        from vision_pipeline.gemini_extractor import GeminiExtractor
        cfg = dict(self._gemini_cfg)
        cfg["gemini_model"] = model
        return GeminiExtractor(key, cfg)

    # ── Public interface ──────────────────────────────────────────────────

    def describe(self) -> str:
        if self._provider != "gemini" or not self._keys:
            return f"Groq ({self._cfg.get('groq', {}).get('vision_model', 'llama-4-scout')})"
        return f"Gemini tiers {GEMINI_TIERS} — {len(self._keys)} key(s) + Groq fallback"

    def active_provider(self) -> str:
        if self._using_groq or self._provider != "gemini":
            return "groq"
        if self._tier_idx < len(GEMINI_TIERS):
            model = GEMINI_TIERS[self._tier_idx]
            state = self._tier_state[model]
            return f"{model}[key{state['active_idx'] + 1}]"
        return "groq"

    def extract_page(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        if self._provider != "gemini" or not self._keys or self._using_groq:
            return self._groq_extract(png_path, page_num)

        # Try each tier in order, but only skip tiers that are quota-exhausted (429)
        # Overloaded tiers (503) are retried each page since it's a temporary server issue
        for tier_offset in range(len(GEMINI_TIERS)):
            actual_idx = (self._tier_idx + tier_offset) % len(GEMINI_TIERS)
            model = GEMINI_TIERS[actual_idx]
            state = self._tier_state[model]

            # Skip tier only if ALL keys are quota-exhausted (429), not just overloaded
            all_exhausted = len(state["exhausted_keys"]) >= len(self._keys)
            if all_exhausted:
                continue

            # Clear overloaded set each page — 503 is temporary
            state["overloaded_keys"].clear()

            result = self._try_tier(model, png_path, page_num)

            if result is None:
                # All keys quota-exhausted on this tier — advance permanently
                if len(state["exhausted_keys"]) >= len(self._keys):
                    if actual_idx == self._tier_idx:
                        self._tier_idx = min(self._tier_idx + 1, len(GEMINI_TIERS))
                        if self._tier_idx < len(GEMINI_TIERS):
                            print(f"\n  All {model} keys quota exhausted — switching to {GEMINI_TIERS[self._tier_idx]}")
                continue

            if result == [] and png_path.stat().st_size > 100_000:
                print(f"  [page {page_num}] {model} returned empty on large page ({png_path.stat().st_size // 1024} KB) — trying Groq...")
                groq_result = self._groq_extract(png_path, page_num)
                if groq_result:
                    return groq_result

            return result

        # All Gemini tiers quota-exhausted
        print("\n  All Gemini tiers exhausted — switching to Groq permanently")
        self._using_groq = True
        return self._groq_extract(png_path, page_num)

    # ── Per-tier logic ────────────────────────────────────────────────────

    def _try_tier(self, model: str, png_path: Path, page_num: int) -> list[dict[str, Any]] | None:
        """
        Try all available keys for a given model tier.
        Returns:
          - list of products (possibly empty) on success
          - None if all keys are exhausted/failed for this tier
        """
        state = self._tier_state[model]
        num_keys = len(self._keys)
        keys_attempted = 0

        # Build a list of key indices to try: start from active_idx, skip exhausted
        for attempt in range(num_keys):
            idx = (state["active_idx"] + attempt) % num_keys

            if idx in state["exhausted_keys"]:
                keys_attempted += 1
                continue

            key = self._keys[idx]
            extractor = self._make_extractor(key, model)

            try:
                result = extractor.extract_page(png_path, page_num)
                # Success — update active index and return
                state["active_idx"] = idx
                state["overloaded_keys"].discard(idx)
                return result

            except Exception as exc:
                err = str(exc).lower()
                is_quota = "429" in err or "quota" in err
                is_overload = "503" in err or "unavailable" in err
                is_not_found = "404" in err or "not_found" in err

                if is_not_found:
                    # Model doesn't exist — skip entire tier
                    print(f"\n  {model} not available (404) — skipping tier")
                    return None

                if is_quota:
                    state["exhausted_keys"].add(idx)
                    keys_attempted += 1
                    remaining = num_keys - len(state["exhausted_keys"])
                    if remaining > 0:
                        next_idx = self._next_available(state, num_keys)
                        print(f"\n  {model} key {idx+1} quota exhausted — rotating to key {next_idx+1}")
                        state["active_idx"] = next_idx
                    else:
                        print(f"\n  All {model} keys quota exhausted")
                        return None

                elif is_overload:
                    state["overloaded_keys"].add(idx)
                    keys_attempted += 1
                    next_idx = self._next_available(state, num_keys)
                    if next_idx != idx:
                        print(f"\n  [page {page_num}] {model} key {idx+1} overloaded (503) — trying key {next_idx+1}")
                        state["active_idx"] = next_idx
                    else:
                        # All keys overloaded — bail to next tier
                        print(f"\n  [page {page_num}] All {model} keys overloaded — trying next tier")
                        return None
                else:
                    # Other error — don't rotate, return empty
                    print(f"  [page {page_num}] {model} error: {exc}")
                    return []

        # All keys tried and exhausted
        return None

    def _next_available(self, state: dict, num_keys: int) -> int:
        """Find next key index not in exhausted_keys."""
        for i in range(num_keys):
            idx = (state["active_idx"] + 1 + i) % num_keys
            if idx not in state["exhausted_keys"]:
                return idx
        return state["active_idx"]  # fallback to current if all exhausted

    def _groq_extract(self, png_path: Path, page_num: int) -> list[dict[str, Any]]:
        if self._groq_extractor is None:
            print(f"  [page {page_num}] No Groq fallback available")
            return []
        return self._groq_extractor.extract_page(png_path, page_num)
