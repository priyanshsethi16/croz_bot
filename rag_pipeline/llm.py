"""
rag_pipeline/llm.py
--------------------
LangChain-based chat pipeline:
  - ChatGroq (Qwen3-32b) as the chat LLM
  - ConversationBufferWindowMemory (k=1) — remembers last exchange
  - Grounded prompt: retrieved context + user query
"""
from __future__ import annotations

import os
import re
from typing import Any

from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, AIMessage

_SYSTEM = """You are GROZ Assistant — an expert chatbot exclusively for the GROZ industrial tools and equipment product catalog.

Your ONLY job is to answer questions about GROZ products: tools, equipment, specifications, product codes, ordering info, variants, pressure ratings, dimensions.

STRICT RULES:
1. If the user's question is NOT about GROZ industrial products, tools, or equipment — respond ONLY with:
   "I'm only able to help with GROZ industrial product queries. Please ask about a product, specification, or catalog item."
2. Answer using ONLY product information present in CATALOG CONTEXT or PREVIOUS ANSWER
3. When the user refers to "Product 1", "Product 2", etc. or "above", look it up in PREVIOUS ANSWER first
4. Include product codes, cat numbers, specs, pressure ratings, dimensions
5. If multiple products match, list all with their codes
6. If a product question has no match in context, say: "I don't have information about that in the current catalog"
7. Never invent product codes, specs, or prices
8. Format ALL structured data (ordering info, specs, variants) as markdown tables"""


def _build_context(chunks: list[dict[str, Any]]) -> str:
    parts = []
    for i, chunk in enumerate(chunks, 1):
        meta   = chunk["metadata"]
        name   = meta.get("product_name", "")
        code   = meta.get("product_code", "")
        score  = chunk["score"]
        parts.append(f"[Product {i}] {name} | Code: {code} | Relevance: {score}\n\n{chunk['text']}")
    sep = "\n\n" + "=" * 60 + "\n\n"
    return sep.join(parts)


def _bullets_to_table(text: str) -> str:
    lines   = text.split("\n")
    result: list[str] = []
    block:  list[tuple[str, str]] = []
    bullet_re = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*[:\s]+(.*)$")

    def flush():
        if len(block) < 2:
            for k, v in block:
                result.append(f"- **{k}:** {v}")
        else:
            result.append("| " + " | ".join(k for k, _ in block) + " |")
            result.append("| " + " | ".join("---" for _ in block) + " |")
            result.append("| " + " | ".join(v for _, v in block) + " |")
        block.clear()

    for line in lines:
        m = bullet_re.match(line)
        if m:
            block.append((m.group(1).strip(), m.group(2).strip()))
        else:
            if block:
                flush()
            result.append(line)
    if block:
        flush()
    return "\n".join(result)


class LLMAnswerer:
    """
    Stateful chat answerer with conversation buffer memory (k=1).
    Each instance holds one conversation session.
    """

    def __init__(self, groq_api_key: str, model: str = "qwen/qwen3-32b"):
        self._llm = ChatGroq(
            api_key=groq_api_key,
            model=model,
            temperature=0.1,
            max_tokens=1024,
        )
        self._history: list = []  # keeps last 1 exchange (2 messages)

    def answer(self, query: str, chunks: list[dict[str, Any]]) -> str:
        if not chunks:
            return "No relevant products found in the catalog for your query."

        context = _build_context(chunks)

        # Build user message: include previous answer if available so LLM can
        # correctly resolve references like "Product 1" or "above"
        prev_answer = self._history[-1].content if self._history else None
        if prev_answer:
            user_message = (
                f"PREVIOUS ANSWER:\n{prev_answer}\n\n"
                f"CATALOG CONTEXT:\n\n{context}\n\nQUESTION: {query}"
            )
        else:
            user_message = f"CATALOG CONTEXT:\n\n{context}\n\nQUESTION: {query}"

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=_SYSTEM),
            ("human", "{input}"),
        ])

        chain    = prompt | self._llm
        response = chain.invoke({"input": user_message})
        raw      = response.content or ""
        raw      = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        answer   = _bullets_to_table(raw)

        # Store only the last AI answer for next-turn reference
        self._history = [AIMessage(content=answer)]
        return answer

    def clear_memory(self):
        self._history.clear()
