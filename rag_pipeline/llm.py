"""
rag_pipeline/llm.py
--------------------
Builds a grounded prompt from retrieved .md chunks and calls the configured
OpenAI or Google Gemini chat model through LangChain.
Context is the raw markdown — no field assumptions, works for any PDF layout.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

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


def _message_text(message) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


class LLMAnswerer:
    def __init__(self, provider: str, api_key: str, model: str):
        self.provider = provider.lower().strip()
        self.model = model
        if not api_key:
            raise ValueError(f"API key is required for {self.provider} chat.")
        if self.provider == "openai":
            self._model = ChatOpenAI(model=model, api_key=api_key, max_retries=3)
        elif self.provider == "gemini":
            self._model = ChatGoogleGenerativeAI(
                model=model,
                api_key=api_key,
                temperature=0.1,
                max_retries=3,
            )
        else:
            raise ValueError("Chat provider must be either openai or gemini.")

    def answer(self, query: str, chunks: list[dict[str, Any]]) -> str:
        if not chunks:
            return "No relevant products found in the catalog for your query."

        context      = build_context(chunks)
        user_message = f"CATALOG CONTEXT:\n\n{context}\n\nQUESTION: {query}"

        response = self._model.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ])
        raw = _message_text(response)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        return _bullets_to_table(raw)
