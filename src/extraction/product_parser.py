"""
Product parser — converts a product boundary's raw OCR text
into a structured Product object using Groq / Qwen.
"""
from __future__ import annotations

import json
from typing import Any

from groq import Groq

from src.config import LLMConfig, ProcessingConfig
from src.extraction.boundary_detector import ProductBoundary
from src.models.product import Product
from src.utils.llm_utils import call_groq, repair_json, truncate_text
from src.utils.logger import logger


EXTRACTION_PROMPT = """You are an expert industrial product catalog parser.

Extract ALL structured information for this product family from the OCR text below.
Never drop rows from ordering or size tables. Never omit feature bullets or utilities.

OCR TEXT:
{text}

Return a single JSON object (no markdown fences) matching this schema exactly:
{{
  "product_id": "",
  "product_name": "",
  "product_code": "",
  "page_start": {page_start},
  "page_end": {page_end},
  "category": "",
  "description": "",
  "features": ["..."],
  "utilities": ["..."],
  "specifications": {{
    "key": "value"
  }},
  "variants": [
    {{"name": "Size", "value": "500g", "unit": "g"}}
  ],
  "ordering_information": [
    {{"Cat No": "...", "Ord No": "...", "Weight": "...", "Dimensions": "..."}}
  ],
  "children": [
    {{
      "product_code": "BPID-SF",
      "product_name": "Ball Pein Hammer Soft Face",
      "description": "",
      "features": [],
      "variants": [],
      "ordering_information": []
    }}
  ],
  "raw_text": ""
}}

Guidelines:
- product_name: Full product family name (e.g. "Ball Pein Hammers - Indestructible Handle")
- product_code: Primary family code like CHID, BPID (empty string if not found)
- category: Infer from context (e.g. "Hammers", "Chisels", "Files")
- description: Complete product description from OCR
- features: Every feature bullet as a separate string
- utilities: Every utility / application bullet as a separate string
- specifications: All key-value specs (material, standard, finish, hardness, etc.)
- variants: All size/weight variants; include table rows as variants when applicable
- ordering_information: EVERY row from ordering/catalog tables — preserve all columns
- children: Child variants (e.g. BPID-SF under BPID family) with their own features/tables
- raw_text: Leave as empty string ""
- If a field has no data, use "" or [] or {{}}
- Return ONLY valid JSON, no explanation, no markdown
"""

# Product header pages are the first page(s) that introduce the product.
# Every text chunk must include this header page so the LLM always has full
# context (product name, code, description) regardless of which chunk it sees.
_HEADER_PAGES = 1   # Number of leading pages kept in every split chunk


class ProductParser:
    """Parse a ProductBoundary into a structured Product using Groq / Qwen."""

    def __init__(self, config: LLMConfig, processing_config: ProcessingConfig | None = None):
        self.config = config
        self.processing = processing_config or ProcessingConfig()
        if not config.groq_api_key:
            raise ValueError("GROQ_API_KEY is not set.")
        self.client = Groq(api_key=config.groq_api_key)

    def _split_text_chunks(self, text: str) -> list[str]:
        """
        Split OCR text at page breaks to stay within prompt char limits.

        The product header page (first ``_HEADER_PAGES`` pages) is prepended
        to every chunk so the LLM always has the product identity in context.
        """
        max_ocr = self.processing.max_ocr_chars
        if len(text) <= max_ocr:
            return [text]

        sep = "\n\n---PAGE BREAK---\n\n"
        pages = text.split(sep)

        # Identify header pages — keep them for re-injection
        header_pages = pages[:_HEADER_PAGES]
        header_text = sep.join(header_pages)
        header_len = len(header_text)

        body_pages = pages[_HEADER_PAGES:]
        chunks: list[str] = []
        current: list[str] = []
        # Account for the header that will be prepended
        current_len = header_len + (len(sep) if body_pages else 0)

        for page in body_pages:
            page_len = len(page)
            sep_cost = len(sep) if current else 0
            if current and current_len + sep_cost + page_len > max_ocr:
                # Flush current chunk, always prepend header
                chunk_body = sep.join(current)
                chunks.append(header_text + sep + chunk_body if chunk_body else header_text)
                current = [page]
                current_len = header_len + len(sep) + page_len
            else:
                current.append(page)
                current_len += sep_cost + page_len

        if current:
            chunk_body = sep.join(current)
            chunks.append(header_text + sep + chunk_body if chunk_body else header_text)
        elif not chunks:
            # Entire text (with header) fits in one piece
            chunks = [truncate_text(text, max_ocr)]

        return chunks

    def _build_prompt(self, text: str, boundary: ProductBoundary) -> str:
        prompt = EXTRACTION_PROMPT.format(
            text=text,
            page_start=boundary.page_start,
            page_end=boundary.page_end,
        )
        if len(prompt) > self.processing.max_prompt_chars:
            overflow = len(prompt) - self.processing.max_prompt_chars
            trimmed = truncate_text(text, max(400, len(text) - overflow))
            prompt = EXTRACTION_PROMPT.format(
                text=trimmed,
                page_start=boundary.page_start,
                page_end=boundary.page_end,
            )
        return prompt

    def _call_and_parse(self, prompt: str) -> dict[str, Any]:
        # Estimate prompt tokens (≈ chars / 3.5 is a safe upper bound for English+JSON)
        estimated_prompt_tokens = int(len(prompt) / 3.5) + 64  # 64-token safety margin
        # Groq free tier hard cap per request: leave room for the prompt
        tpm_cap = getattr(self.processing, "groq_tpm_limit", 6000)
        available = max(256, tpm_cap - estimated_prompt_tokens)
        # Also respect the configured extraction_max_tokens ceiling
        max_tok = min(self.processing.extraction_max_tokens, available)

        raw = call_groq(
            self.client,
            self.config,
            prompt,
            max_tokens=max_tok,
            inter_call_delay=self.processing.llm_call_delay,
        )
        logger.debug(f"  extraction max_tokens={max_tok} (prompt≈{estimated_prompt_tokens} toks)")
        parsed = repair_json(raw, expect_array=False)
        if not isinstance(parsed, dict):
            raise ValueError("Extraction response is not a JSON object")
        return parsed

    def _merge_partials(self, parts: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Merge extraction results from multiple text chunks.

        List fields (features, utilities, ordering_information, variants,
        children) are concatenated with deduplication where appropriate.
        Scalar fields use the first non-empty value seen.
        """
        if not parts:
            return {}
        if len(parts) == 1:
            return parts[0]

        merged: dict[str, Any] = dict(parts[0])

        for part in parts[1:]:
            # Deduplicated list fields
            for key in ("features", "utilities"):
                existing: list = merged.get(key) or []
                incoming: list = part.get(key) or []
                if isinstance(existing, list) and isinstance(incoming, list):
                    seen = {str(x).strip() for x in existing}
                    for item in incoming:
                        s = str(item).strip()
                        if s and s not in seen:
                            existing.append(item)
                            seen.add(s)
                    merged[key] = existing

            # Concatenated list fields (ordering rows, variants, children)
            for key in ("ordering_information", "variants", "children"):
                existing = merged.get(key) or []
                incoming = part.get(key) or []
                if isinstance(existing, list) and isinstance(incoming, list):
                    # Deduplicate ordering rows by their string representation
                    seen_rows = {str(r) for r in existing}
                    new_rows = [r for r in incoming if str(r) not in seen_rows]
                    merged[key] = existing + new_rows

            # Merge specification dicts — later chunks may add new keys
            specs: dict = merged.get("specifications") or {}
            part_specs: dict = part.get("specifications") or {}
            if isinstance(specs, dict) and isinstance(part_specs, dict):
                specs.update({k: v for k, v in part_specs.items() if v})
                merged["specifications"] = specs

            # Scalar fall-through: keep first non-empty value
            for key in ("description", "category", "product_name", "product_code"):
                if not merged.get(key) and part.get(key):
                    merged[key] = part[key]

        return merged

    def _parse_response(self, data: dict[str, Any], boundary: ProductBoundary) -> Product:
        data["raw_text"] = boundary.combined_text()
        data["page_start"] = boundary.page_start
        data["page_end"] = boundary.page_end

        if not data.get("product_name") and boundary.candidate_title:
            data["product_name"] = boundary.candidate_title
        if not data.get("product_code") and boundary.candidate_code:
            data["product_code"] = boundary.candidate_code

        if "children" not in data:
            data["children"] = []

        return Product.model_validate(data)

    def parse(self, boundary: ProductBoundary, index: int) -> Product:
        """Parse one product boundary → Product."""
        full_text = boundary.combined_text()
        chunks = self._split_text_chunks(full_text)

        logger.debug(
            f"Parsing pages {boundary.page_start}-{boundary.page_end}: "
            f"{len(chunks)} chunk(s), max_tokens=16384"
        )

        try:
            partials: list[dict[str, Any]] = []
            for chunk_idx, chunk in enumerate(chunks):
                prompt = self._build_prompt(chunk, boundary)
                partial = self._call_and_parse(prompt)
                logger.debug(
                    f"  Chunk {chunk_idx + 1}/{len(chunks)}: "
                    f"{len(partial.get('ordering_information') or [])} ordering rows, "
                    f"{len(partial.get('children') or [])} children"
                )
                partials.append(partial)

            data = self._merge_partials(partials)
            product = self._parse_response(data, boundary)

            if not product.product_id:
                code = (product.product_code or "UNK").replace("-", "_").lower()
                product.product_id = f"PROD_{index:04d}_{code}"[:40]

            logger.debug(
                f"Parsed: [{product.product_id}] {product.product_name} "
                f"(pages {boundary.page_start}-{boundary.page_end}, "
                f"{len(chunks)} chunk(s))"
            )
            return product

        except Exception as exc:
            logger.error(
                f"Product parse failed for pages {boundary.page_start}-"
                f"{boundary.page_end}: {exc}"
            )
            return Product(
                product_id=f"PROD_{index:04d}",
                product_name=boundary.candidate_title or f"Unknown Product {index}",
                product_code=boundary.candidate_code,
                page_start=boundary.page_start,
                page_end=boundary.page_end,
                raw_text=full_text,
            )
