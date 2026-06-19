"""
rag_pipeline/llm.py
--------------------
Builds a grounded prompt from retrieved .md chunks and calls Groq (Qwen3-32b).
Context is the raw markdown — no field assumptions, works for any PDF layout.
"""
from __future__ import annotations

import os
import re
from typing import Any

from groq import Groq

SYSTEM_PROMPT = """You are an expert industrial product catalog assistant.

Answer the user's question using ONLY the product information provided in the context below.
- Be specific: include product codes, cat numbers, order numbers, specs, pressure ratings, dimensions
- If multiple products match, list all of them with their codes
- If the answer is not in the context, say exactly: "I don't have information about that in the current catalog in case nothing is there"
- Never invent product codes, specs, or prices
- Keep answers concise but complete
- You MUST format ALL structured data (ordering info, variants, specifications, key details, dimensions) as a markdown table with | header | columns |. Never use bullet points like "- **Key:** Value" for structured data."""


def _bullets_to_table(text: str) -> str:
    """
    Convert any block of "- **Key:** Value" bullet lines into a markdown table.
    Leaves prose paragraphs and existing tables untouched.
    """
    lines = text.split("\n")
    result: list[str] = []
    block: list[tuple[str, str]] = []   # (key, value) pairs collected

    def flush_block():
        if len(block) < 2:
            # Single item — just emit as-is
            for k, v in block:
                result.append(f"- **{k}:** {v}")
        else:
            result.append("| " + " | ".join(k for k, _ in block) + " |")
            result.append("| " + " | ".join("---" for _ in block) + " |")
            result.append("| " + " | ".join(v for _, v in block) + " |")
        block.clear()

    bullet_re = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*[:\s]+(.*)$")

    for line in lines:
        m = bullet_re.match(line)
        if m:
            block.append((m.group(1).strip(), m.group(2).strip()))
        else:
            if block:
                flush_block()
            result.append(line)

    if block:
        flush_block()

    return "\n".join(result)


def build_context(chunks: list[dict[str, Any]]) -> str:
    """
    Format retrieved chunks into a numbered context block.
    Uses full .md text — works for any product structure.
    """
    parts = []
    for i, chunk in enumerate(chunks, 1):
        meta   = chunk["metadata"]
        name   = meta.get("product_name", "")
        code   = meta.get("product_code", "")
        score  = chunk["score"]
        header = f"[Product {i}] {name} | Code: {code} | Relevance: {score}"
        parts.append(f"{header}\n\n{chunk['text']}")
    return "\n\n{'='*60}\n\n".join(parts)


class LLMAnswerer:
    def __init__(self, groq_api_key: str, model: str = "qwen/qwen3-32b"):
        self._client = Groq(api_key=groq_api_key)
        self.model   = model

    def answer(self, query: str, chunks: list[dict[str, Any]]) -> str:
        if not chunks:
            return "No relevant products found in the catalog for your query."

        context      = build_context(chunks)
        user_message = f"CATALOG CONTEXT:\n\n{context}\n\nQUESTION: {query}"

        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            temperature=0.1,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or ""
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return _bullets_to_table(raw)
