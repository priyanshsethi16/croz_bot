"""
chunk_writer.py
---------------
Converts a product family dict (from vision_extractor) into a rich markdown file.

One .md file per product FAMILY.
Children (sub-variants) get their own ### sub-sections with all attributes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    s = str(text).lower()
    s = re.sub(r"[^a-z0-9\s\-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s.strip())
    return s[:60] or "unknown"


def _render_table(rows: list[Any]) -> str:
    """Render list-of-dicts as a markdown table with dynamic column detection."""
    if not rows:
        return ""
    # Accept both dicts and plain strings
    if not isinstance(rows[0], dict):
        return "\n".join(f"- {r}" for r in rows if str(r).strip())

    # Collect all keys, preserving insertion order, skip internal keys
    _skip = {"page_num", "bestseller"}
    cols: dict[str, None] = {}
    for row in rows:
        cols.update({k: None for k in row if k not in _skip})
    columns = list(cols.keys())
    if not columns:
        return ""

    header = "| " + " | ".join(columns) + " |"
    sep    = "| " + " | ".join(["---"] * len(columns)) + " |"
    data_rows = []
    for row in rows:
        cells = []
        for k in columns:
            val = str(row.get(k, ""))
            # Mark bestseller rows with a star
            if row.get("bestseller") and k == columns[0]:
                val = f"★ {val}"
            cells.append(val)
        data_rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, sep] + data_rows)


def _render_specs(specs: dict) -> str:
    """Render specs as a markdown table (3+ entries) or bullet list."""
    if not specs:
        return ""
    items = [(k, v) for k, v in specs.items() if v]
    if not items:
        return ""
    if len(items) >= 3:
        lines = ["| Specification | Value |", "| --- | --- |"]
        lines += [f"| **{k}** | {v} |" for k, v in items]
        return "\n".join(lines)
    return "\n".join(f"- **{k}:** {v}" for k, v in items)


def _render_sizes(sizes: list) -> str:
    """Render a sizes list — table if dicts, bullet list if strings."""
    if not sizes:
        return ""
    if isinstance(sizes[0], dict):
        return _render_table(sizes)
    return "\n".join(f"- {s}" for s in sizes if str(s).strip())


# ── Child section renderer ────────────────────────────────────────────────────

def _render_child(child: dict[str, Any]) -> list[str]:
    """Render one child variant as a ### sub-section with all its attributes."""
    lines: list[str] = []

    code = str(child.get("product_code") or "").strip()
    name = str(child.get("product_name") or "").strip()

    # Heading is always the product code when available — it's the identifier
    # The descriptive name (e.g. "Fits 20-30 kg pails") goes below as a label
    heading = code if code else name
    if not heading:
        return lines

    lines.append(f"### {heading}")
    lines.append("")

    # Show descriptive name below the code heading if it differs from the code
    if name and name.lower() != code.lower():
        lines.append(f"**{name}**")
        lines.append("")

    # Description
    desc = str(child.get("description") or "").strip()
    if desc:
        lines.append(desc)
        lines.append("")

    # Features
    features: list = child.get("features") or []
    if features:
        lines.append("#### Features")
        lines.append("")
        for f in features:
            s = str(f).strip()
            if s:
                lines.append(f"- {s}")
        lines.append("")

    # Utilities
    utilities: list = child.get("utilities") or []
    if utilities:
        lines.append("#### Utility")
        lines.append("")
        for u in utilities:
            s = str(u).strip()
            if s:
                lines.append(f"- {s}")
        lines.append("")

    # Specifications
    specs: dict = child.get("specifications") or {}
    if specs:
        lines.append("#### Specifications")
        lines.append("")
        lines.append(_render_specs(specs))
        lines.append("")

    # Sizes
    sizes: list = child.get("sizes") or []
    if sizes:
        lines.append("#### Sizes")
        lines.append("")
        lines.append(_render_sizes(sizes))
        lines.append("")

    # Ordering table
    ordering: list = child.get("ordering_table") or []
    if ordering:
        lines.append("#### Ordering Information")
        lines.append("")
        lines.append(_render_table(ordering))
        lines.append("")

    return lines


# ── Main markdown generator ───────────────────────────────────────────────────

def generate_markdown(product: dict[str, Any]) -> str:
    """
    Convert a product family dict into a full markdown document.

    Structure:
      # Family Name
      product code, category, source page
      ## Description
      ## Features
      ## Utility
      ## Specifications
      ## Variants / Children
        ### Child 1
          code, description, features, specs, sizes, ordering table
        ### Child 2
          ...
      ## Notes
    """
    lines: list[str] = []

    name     = str(product.get("product_name") or "Unknown Product").strip()
    code     = str(product.get("product_code") or "").strip()
    category = str(product.get("category") or "").strip()
    page_num = product.get("page_num", "")
    desc     = str(product.get("description") or "").strip()
    features: list = product.get("features") or []
    utilities: list = product.get("utilities") or []
    specs: dict     = product.get("specifications") or {}
    notes    = str(product.get("notes") or "").strip()
    children: list  = product.get("children") or []

    # ── Title ──────────────────────────────────────────────────────────────
    lines.append(f"# {name}")
    lines.append("")

    if code:
        lines.append(f"**Product Code:** `{code}`")
    if category:
        lines.append(f"**Category:** {category}")
    if page_num:
        lines.append(f"**Source Page:** {page_num}")
    if code or category or page_num:
        lines.append("")

    # ── Description ────────────────────────────────────────────────────────
    if desc:
        lines.append("## Description")
        lines.append("")
        lines.append(desc)
        lines.append("")

    # ── Features ───────────────────────────────────────────────────────────
    if features:
        lines.append("## Features")
        lines.append("")
        for f in features:
            s = str(f).strip()
            if s:
                lines.append(f"- {s}")
        lines.append("")

    # ── Utility ────────────────────────────────────────────────────────────
    if utilities:
        lines.append("## Utility")
        lines.append("")
        for u in utilities:
            s = str(u).strip()
            if s:
                lines.append(f"- {s}")
        lines.append("")

    # ── Specifications ─────────────────────────────────────────────────────
    if specs:
        lines.append("## Specifications")
        lines.append("")
        lines.append(_render_specs(specs))
        lines.append("")

    # ── Children / Variants ────────────────────────────────────────────────
    if children:
        lines.append("## Variants")
        lines.append("")
        for child in children:
            child_lines = _render_child(child)
            if child_lines:
                lines.extend(child_lines)
                lines.append("---")
                lines.append("")

    # ── Notes ──────────────────────────────────────────────────────────────
    if notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(notes)
        lines.append("")

    return "\n".join(lines)


# ── File saver ────────────────────────────────────────────────────────────────

def save_chunk(
    product: dict[str, Any],
    index: int,
    chunks_dir: str,
) -> Path:
    """Mock saving to disk: generate markdown path but do NOT save to file."""
    # Prefer code for filename, fall back to name
    code = str(product.get("product_code") or "").strip()
    name = str(product.get("product_name") or "unknown").strip()
    slug_text = code if code else name
    filename = f"{index:04d}_{_slug(slug_text)}.md"

    path = Path(chunks_dir) / filename
    return path
