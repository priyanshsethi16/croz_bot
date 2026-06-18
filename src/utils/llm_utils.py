"""Shared Groq / Qwen helpers: retries, JSON repair, prompt sizing."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from groq import Groq

from src.config import LLMConfig
from src.utils.logger import logger


def strip_llm_wrapper(text: str) -> str:
    """Remove markdown fences, thinking blocks, and leading noise."""
    if not text:
        return ""
    cleaned = text.strip()
    # Strip Qwen3 thinking blocks if present
    think_open_tag = "<" + "think" + ">"
    think_close_tag = "</" + "think" + ">"
    lower = cleaned.lower()
    think_open = lower.find(think_open_tag)
    if think_open >= 0:
        think_close = lower.find(think_close_tag, think_open + len(think_open_tag))
        if think_close >= 0:
            cleaned = cleaned[:think_open] + cleaned[think_close + len(think_close_tag):]
        else:
            cleaned = cleaned[:think_open]
    cleaned = re.sub(r"```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")
    return cleaned.strip()


def _extract_json_span(text: str, expect_array: bool) -> str:
    """Extract the outermost JSON array or object from text."""
    if expect_array:
        start = text.find("[")
        end = text.rfind("]")
    else:
        start = text.find("{")
        end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def repair_json(text: str, expect_array: bool = False) -> Any:
    """
    Parse LLM JSON output with light repair for common formatting issues.
    Raises json.JSONDecodeError if unrecoverable.
    """
    cleaned = strip_llm_wrapper(text)
    candidate = _extract_json_span(cleaned, expect_array)

    # Remove trailing commas before } or ]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Retry after stripping control characters outside strings (conservative)
        compact = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", candidate)
        return json.loads(compact)


def truncate_text(text: str, max_chars: int, suffix: str = "\n...[truncated]") -> str:
    """Truncate text to a character budget."""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def call_groq(
    client: Groq,
    config: LLMConfig,
    prompt: str,
    *,
    max_tokens: int | None = None,
    inter_call_delay: float = 0.5,
) -> str:
    """Call Groq chat completions with retries and backoff."""
    tokens = max_tokens or config.max_tokens
    last_error: Exception | None = None

    for attempt in range(1, config.max_retries + 1):
        try:
            if attempt > 1:
                time.sleep(config.retry_delay * attempt)
            elif inter_call_delay > 0:
                time.sleep(inter_call_delay)

            response = client.chat.completions.create(
                model=config.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=config.temperature,
                max_tokens=tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            last_error = exc
            logger.warning(f"Groq call attempt {attempt}/{config.max_retries} failed: {exc}")

    if last_error:
        raise last_error
    return ""
