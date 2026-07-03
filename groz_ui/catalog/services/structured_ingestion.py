"""Persist validated assembled products without making embedding API calls."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Any, Iterable

from django.db import transaction

from catalog.models import (
    CatalogDocument,
    DocumentChunk,
    ProductFamily,
    ProductVariant,
)
from catalog.services.taxonomy import resolve_category
from vision_pipeline.chunk_writer import generate_markdown


APPROVAL_CONFIDENCE = 0.8


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def _hash(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _clean(value: Any) -> str:
    return str(value or '').strip()


def _row_value(row: dict[str, Any], *names: str) -> str:
    normalized = {re.sub(r'[^a-z0-9]', '', str(key).lower()): value for key, value in row.items()}
    for name in names:
        value = normalized.get(re.sub(r'[^a-z0-9]', '', name.lower()))
        if value not in (None, ''):
            return _clean(value)
    return ''


def _variant_rows(product: dict[str, Any]) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    children = product.get('children') or []

    for child in children:
        if not isinstance(child, dict):
            continue
        ordering_rows = child.get('ordering_table') or []
        size_rows = child.get('sizes') or []
        rows = ordering_rows or size_rows or [{}]
        for row in rows:
            row = row if isinstance(row, dict) else {'value': row}
            cat_number = _row_value(row, 'cat_no', 'catalog_no', 'product_code', 'code')
            order_number = _row_value(row, 'ord_no', 'order_no', 'order_number')
            product_code = cat_number or _clean(child.get('product_code')) or _clean(product.get('product_code'))
            name = _clean(child.get('product_name')) or _clean(product.get('product_name'))
            size = _row_value(row, 'size', 'dimensions', 'dimension')
            unit = _row_value(row, 'unit')
            source = {
                'child_code': _clean(child.get('product_code')),
                'child_name': _clean(child.get('product_name')),
                'row': row,
            }
            source_row_hash = _hash(source)
            if source_row_hash in seen:
                continue
            seen.add(source_row_hash)
            variants.append({
                'product_code': product_code,
                'order_number': order_number,
                'name': name,
                'size': size,
                'unit': unit,
                'specifications': deepcopy(child.get('specifications') or {}),
                'ordering_data': deepcopy(row),
                'source_row_hash': source_row_hash,
            })
    return variants


def _family_uuid(document_id: uuid.UUID, source_key: str) -> uuid.UUID:
    return uuid.uuid5(document_id, f'family:{source_key}')


def _variant_uuid(family_id: uuid.UUID, source_row_hash: str) -> uuid.UUID:
    return uuid.uuid5(family_id, f'variant:{source_row_hash}')


def _chunk_uuid(document_id: uuid.UUID, family_id: uuid.UUID, content_hash: str) -> uuid.UUID:
    return uuid.uuid5(document_id, f'chunk:product-family:{family_id}:{content_hash}')


@transaction.atomic
def persist_assembled_products(
    document: CatalogDocument,
    products: Iterable[dict[str, Any]],
    *,
    replace: bool = False,
) -> dict[str, int]:
    """Persist product structure/chunks idempotently; never call an embedder."""
    locked_document = CatalogDocument.objects.select_for_update().get(pk=document.pk)
    if locked_document.is_active and locked_document.product_families.exists():
        raise ValueError('An active document is immutable; create a new catalog version.')
    if replace and locked_document.is_active:
        raise ValueError('An active document cannot be destructively replaced; create a new version.')
    if replace:
        locked_document.product_families.all().delete()

    seen_family_ids: set[uuid.UUID] = set()
    totals = {'families': 0, 'variants': 0, 'chunks': 0, 'needs_review': 0, 'embedding_calls': 0}

    for ordinal, raw_product in enumerate(products):
        product = deepcopy(raw_product)
        source_key = _clean(product.get('source_key'))
        if not source_key:
            fallback = f"{product.get('page_start', 0)}:{product.get('product_code', '')}:{product.get('product_name', '')}"
            source_key = _hash(fallback)[:24]
        family_id = _family_uuid(locked_document.id, source_key)
        seen_family_ids.add(family_id)

        product_name = _clean(product.get('product_name')) or 'Unknown Product'
        raw_category = _clean(product.get('raw_category') or product.get('category'))
        confidence = float(product.get('extraction_confidence') or 0.0)
        normalized_category = resolve_category(
            product_name=product_name,
            raw_category=raw_category,
            suggestion=_clean(product.get('normalized_category_suggestion')),
        )
        automatically_approved = (
            normalized_category is not None
            and confidence >= APPROVAL_CONFIDENCE
            and bool(product.get('children'))
            and not product_name.lower().startswith('unknown product')
        )
        desired_review_status = (
            ProductFamily.ReviewStatus.APPROVED
            if automatically_approved
            else ProductFamily.ReviewStatus.NEEDS_REVIEW
        )

        family, created = ProductFamily.objects.get_or_create(
            id=family_id,
            defaults={
                'document': locked_document,
                'source_key': source_key,
                'product_name': product_name,
            },
        )
        preserve_manual_review = not created and family.review_status == ProductFamily.ReviewStatus.APPROVED
        family.document = locked_document
        family.source_key = source_key
        family.product_name = product_name
        family.product_code = _clean(product.get('product_code'))
        family.raw_category = raw_category
        aliases = product.get('aliases') or product.get('alias') or []
        if isinstance(aliases, str):
            aliases = [aliases]
        family.aliases = [_clean(alias) for alias in aliases if _clean(alias)]
        if not preserve_manual_review:
            family.normalized_category = normalized_category
            family.review_status = desired_review_status
        family.description = _clean(product.get('description'))
        family.features = product.get('features') or []
        family.utilities = product.get('utilities') or []
        family.materials = product.get('materials') or {}
        family.specifications = product.get('specifications') or {}
        family.page_start = int(product.get('page_start') or product.get('original_page_num') or 0)
        family.page_end = int(product.get('page_end') or family.page_start)
        family.extraction_confidence = confidence
        family.raw_extraction = product
        family.save()

        family.variants.all().delete()
        family.chunks.all().delete()
        for variant_data in _variant_rows(product):
            ProductVariant.objects.create(
                id=_variant_uuid(family.id, variant_data['source_row_hash']),
                family=family,
                product_code=variant_data['product_code'],
                order_number=variant_data['order_number'],
                name=variant_data['name'],
                size=variant_data['size'],
                unit=variant_data['unit'],
                specifications=variant_data['specifications'],
                ordering_data=variant_data['ordering_data'],
                page_start=family.page_start,
                page_end=family.page_end,
                source_row_hash=variant_data['source_row_hash'],
            )
            totals['variants'] += 1

        chunk_input = {**product, 'page_num': family.page_start, 'page_start': family.page_start, 'page_end': family.page_end, 'category': raw_category}
        # Legacy Markdown must remain byte-for-byte identical so its existing
        # dense and sparse vectors can be reused by exact content hash.
        chunk_override = product.get('_chunk_text')
        chunk_text = (
            str(chunk_override)
            if chunk_override not in (None, '')
            else generate_markdown(chunk_input)
        )
        content_hash = _hash(chunk_text)
        chunk_id = _chunk_uuid(locked_document.id, family.id, content_hash)
        DocumentChunk.objects.create(
            id=chunk_id,
            document=locked_document,
            family=family,
            chunk_type=DocumentChunk.ChunkType.PRODUCT_FAMILY,
            text=chunk_text,
            page_start=family.page_start,
            page_end=family.page_end,
            content_hash=content_hash,
            ordinal=ordinal,
            qdrant_point_id=chunk_id,
            index_status=DocumentChunk.IndexStatus.PENDING,
            extraction_schema_version=locked_document.extraction_schema_version,
        )
        totals['families'] += 1
        totals['chunks'] += 1
        if family.review_status == ProductFamily.ReviewStatus.NEEDS_REVIEW:
            totals['needs_review'] += 1

    if replace:
        ProductFamily.objects.filter(document=locked_document).exclude(id__in=seen_family_ids).delete()

    locked_document.status = (
        CatalogDocument.Status.REVIEW
        if totals['needs_review']
        else CatalogDocument.Status.INDEXING
    )
    locked_document.failure_summary = ''
    locked_document.save(update_fields=('status', 'failure_summary', 'updated_at'))
    return totals
