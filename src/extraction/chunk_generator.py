"""
Chunk generator — converts a Product into a clean markdown file.
"""
from src.models.product import Product
from src.utils.file_utils import save_chunk
from src.utils.logger import logger

# Minimum number of ordering rows in a child's table before we render it as
# a markdown table instead of a flat list.
_SPECS_TABLE_MIN_ENTRIES = 3


def _detect_columns(rows: list[dict]) -> list[str]:
    """
    Detect table columns dynamically from all row keys, preserving insertion
    order and de-duplicating. Unknown / extra keys from sparse rows are
    appended after the first-seen ordering.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for k in row:
            seen[k] = None
    return list(seen.keys())


def _render_table(rows: list[dict]) -> str:
    """Render list-of-dicts as a markdown table with dynamic column detection."""
    if not rows:
        return ""

    columns = _detect_columns(rows)
    if not columns:
        return ""

    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["------"] * len(columns)) + " |"
    data_rows = [
        "| " + " | ".join(str(row.get(k, "")) for k in columns) + " |"
        for row in rows
    ]

    return "\n".join([header, sep] + data_rows)


def _render_specs_table(specs: dict) -> str:
    """
    Render a specifications dict as a two-column markdown table when it has
    3 or more entries; otherwise fall back to a bullet list.
    """
    if not specs:
        return ""

    items = [(k, v) for k, v in specs.items()]

    if len(items) >= _SPECS_TABLE_MIN_ENTRIES:
        header = "| Specification | Value |"
        sep    = "| ------------- | ----- |"
        rows = [f"| **{k}** | {v} |" for k, v in items]
        return "\n".join([header, sep] + rows)
    else:
        return "\n".join(f"- **{k}:** {v}" for k, v in items)


def _render_child(child: dict) -> list[str]:
    """
    Render a single child variant as a proper sub-section with its own
    heading hierarchy and ordering table.
    """
    lines: list[str] = []

    code = child.get("product_code", "")
    name = child.get("product_name", "")
    heading = name or code
    if not heading:
        return lines

    lines.append(f"### {heading}")
    lines.append("")

    if code and name:
        lines.append(f"**Code:** `{code}`")
        lines.append("")
    elif code:
        lines.append(f"**Code:** `{code}`")
        lines.append("")

    description = child.get("description", "")
    if description:
        lines.append(description)
        lines.append("")

    child_features: list = child.get("features") or []
    if child_features:
        lines.append("#### Features")
        lines.append("")
        for feat in child_features:
            if str(feat).strip():
                lines.append(f"- {str(feat).strip()}")
        lines.append("")

    child_utilities: list = child.get("utilities") or []
    if child_utilities:
        lines.append("#### Utility")
        lines.append("")
        for u in child_utilities:
            if str(u).strip():
                lines.append(f"- {str(u).strip()}")
        lines.append("")

    child_specs: dict = child.get("specifications") or {}
    if child_specs:
        lines.append("#### Specifications")
        lines.append("")
        lines.append(_render_specs_table(child_specs))
        lines.append("")

    child_variants: list = child.get("variants") or []
    if child_variants:
        lines.append("#### Variants")
        lines.append("")
        for v in child_variants:
            if isinstance(v, dict):
                vname  = v.get("name", "")
                vvalue = v.get("value", "")
                vunit  = v.get("unit", "")
                lines.append(f"- {vname}: {vvalue} {vunit}".rstrip())
            else:
                lines.append(f"- {v}")
        lines.append("")

    child_ordering: list = child.get("ordering_information") or []
    if child_ordering:
        lines.append("#### Ordering Information")
        lines.append("")
        lines.append(_render_table(child_ordering))
        lines.append("")

    return lines


def generate_markdown(product: Product) -> str:
    """Generate the full markdown chunk for a product."""
    lines: list[str] = []

    # Title
    lines.append(f"# {product.product_name or 'Unknown Product'}")
    lines.append("")

    if product.product_code:
        lines.append(f"**Product Code:** `{product.product_code}`")
        lines.append("")

    if product.category:
        lines.append(f"**Category:** {product.category}")
        lines.append("")

    # Description
    if product.description:
        lines.append("## Description")
        lines.append("")
        lines.append(product.description)
        lines.append("")

    # Features
    if product.features:
        lines.append("## Features")
        lines.append("")
        for f in product.features:
            if f.strip():
                lines.append(f"- {f.strip()}")
        lines.append("")

    # Utility
    if product.utilities:
        lines.append("## Utility")
        lines.append("")
        for u in product.utilities:
            if u.strip():
                lines.append(f"- {u.strip()}")
        lines.append("")

    # Specifications — table when 3+ entries, bullet list otherwise
    if product.specifications:
        lines.append("## Specifications")
        lines.append("")
        lines.append(_render_specs_table(product.specifications))
        lines.append("")

    # Variants
    if product.variants:
        lines.append("## Variants")
        lines.append("")
        for v in product.variants:
            if v.unit:
                lines.append(f"- {v.name}: {v.value} {v.unit}")
            else:
                lines.append(f"- {v.name}: {v.value}")
        lines.append("")

    # Ordering Information — dynamic column detection
    if product.ordering_information:
        lines.append("## Ordering Information")
        lines.append("")
        lines.append(_render_table(product.ordering_information))
        lines.append("")

    # Child variants — rendered as proper sub-sections
    if product.children:
        lines.append("## Child Variants")
        lines.append("")
        for child in product.children:
            child_lines = _render_child(child)
            if child_lines:
                lines.extend(child_lines)

    # Source Pages
    if product.page_start == product.page_end:
        pages_str = str(product.page_start)
    else:
        pages_str = f"{product.page_start}–{product.page_end}"

    lines.append("## Source Pages")
    lines.append("")
    lines.append(pages_str)
    lines.append("")

    # Raw Product Text (collapsible)
    if product.raw_text:
        lines.append("## Raw Product Text")
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>Show raw OCR text</summary>")
        lines.append("")
        lines.append("```")
        raw = product.raw_text[:3000]
        if len(product.raw_text) > 3000:
            raw += "\n...[truncated]"
        lines.append(raw)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def generate_and_save_chunk(
    product: Product,
    index: int,
    chunks_dir: str = "chunks",
) -> str:
    """Generate markdown, attach to product, save file. Returns file path."""
    md = generate_markdown(product)
    product.markdown_chunk = md

    path = save_chunk(index, product.slug(), md, chunks_dir)
    logger.info(f"  Chunk {index:04d}: {path.name}")
    return str(path)
