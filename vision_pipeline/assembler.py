"""Assemble page-level extractions into stable cross-page product families."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Iterable

from vision_pipeline.schema import ProductExtraction


def _normalized(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def _identity(product: dict[str, Any]) -> str:
    code = _normalized(product.get('product_code', ''))
    if code:
        return f'code:{code}'
    name = _normalized(product.get('product_name', ''))
    category = _normalized(product.get('raw_category') or product.get('category', ''))
    return f'name:{name}|category:{category}'


def _dedupe_list(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _merge_dict(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(left)
    for key, value in right.items():
        if value in ('', None, [], {}):
            continue
        current = merged.get(key)
        if current in ('', None, [], {}):
            merged[key] = deepcopy(value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = _dedupe_list([*current, *value])
        elif key == 'description' and str(value) not in str(current):
            merged[key] = f'{current}\n\n{value}'.strip()
    return merged


def _merge_children(children: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for child in children:
        code = _normalized(child.get('product_code', ''))
        name = _normalized(child.get('product_name', ''))
        key = f'code:{code}' if code else f'name:{name}'
        if key in ('code:', 'name:'):
            key = hashlib.sha256(
                json.dumps(child, sort_keys=True, default=str).encode('utf-8')
            ).hexdigest()
        if key in positions:
            index = positions[key]
            merged[index] = _merge_dict(merged[index], child)
        else:
            positions[key] = len(merged)
            merged.append(deepcopy(child))
    return merged


def _merge_family(base: dict[str, Any], continuation: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_dict(base, continuation)
    pages = sorted(set(base.get('source_pages', []) + continuation.get('source_pages', [])))
    merged['source_pages'] = pages
    merged['page_start'] = min(pages)
    merged['page_end'] = max(pages)
    confidences = base.get('_confidences', []) + continuation.get('_confidences', [])
    merged['_confidences'] = confidences
    merged['extraction_confidence'] = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    merged['children'] = _merge_children([*(base.get('children') or []), *(continuation.get('children') or [])])
    return merged


def assemble_product_families(
    products: Iterable[ProductExtraction | dict[str, Any]],
    *,
    max_page_gap: int = 1,
) -> list[dict[str, Any]]:
    """Merge only matching families on adjacent pages; never guess across gaps."""
    prepared: list[dict[str, Any]] = []
    for product in products:
        data = product.model_dump(mode='json') if isinstance(product, ProductExtraction) else deepcopy(product)
        page = int(data.get('original_page_num') or data.get('page_num') or 0)
        data['raw_category'] = data.get('raw_category') or data.get('category') or ''
        data['source_pages'] = [page]
        data['page_start'] = page
        data['page_end'] = page
        data['_confidences'] = [float(data.get('extraction_confidence') or 0.0)]
        prepared.append(data)

    prepared.sort(key=lambda item: (item['page_start'], _identity(item), item.get('product_name', '')))
    assembled: list[dict[str, Any]] = []

    for product in prepared:
        identity = _identity(product)
        match_index = None
        for index in range(len(assembled) - 1, -1, -1):
            candidate = assembled[index]
            if product['page_start'] - candidate['page_end'] > max_page_gap:
                break
            if candidate['_identity'] == identity:
                match_index = index
                break

        product['_identity'] = identity
        if match_index is None:
            assembled.append(product)
        else:
            assembled[match_index] = _merge_family(assembled[match_index], product)
            assembled[match_index]['_identity'] = identity

    for family in assembled:
        identity = family.pop('_identity')
        family.pop('_confidences', None)
        digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
        family['source_key'] = f"p{family['page_start']}:{digest}"

    return assembled
