"""
rag_pipeline/chatbot.py
-----------------------
CLI chatbot for industrial product catalog queries.

Usage:
    python -m rag_pipeline.chatbot
    python -m rag_pipeline.chatbot --top-k 8
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from rag_pipeline.retriever import Retriever
from rag_pipeline.llm import LLMAnswerer


def run_chatbot(top_k: int = 5):
    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        print("ERROR: GROQ_API_KEY not set in .env")
        return

    print("Loading retriever...")
    try:
        retriever = Retriever(top_k=top_k)
    except Exception as e:
        print(f"ERROR: Could not load ChromaDB collection: {e}")
        print("Run first:  python -m rag_pipeline.ingest")
        return

    llm = LLMAnswerer(groq_key)

    print("\n" + "=" * 60)
    print("  GROZ Industrial Catalog Chatbot")
    print("  Type 'quit' or 'exit' to stop")
    print("  Type 'sources' after any answer to see retrieved chunks")
    print("=" * 60 + "\n")

    last_chunks = []

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit"):
            print("Goodbye!")
            break

        if query.lower() == "sources":
            if not last_chunks:
                print("No previous query.\n")
            else:
                print("\n── Retrieved Sources ──")
                for i, c in enumerate(last_chunks, 1):
                    m = c["metadata"]
                    print(f"  [{i}] {m.get('product_name','')} | Code: {m.get('product_code','')} | Score: {c['score']}")
                print()
            continue

        # Retrieve + answer
        chunks = retriever.retrieve(query)
        last_chunks = chunks
        answer = llm.answer(query, chunks)

        print(f"\nAssistant: {answer}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5, help="Number of chunks to retrieve")
    args = parser.parse_args()
    run_chatbot(args.top_k)
