"""Legacy products-table backfill into review-gated V2 structured records."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from django.db import connection
from django.utils.text import slugify

from catalog.models import Catalog, CatalogDocument
from catalog.services.structured_ingestion import persist_assembled_products
from catalog.models import Category, DocumentChunk, ProductFamily
from catalog.services.taxonomy import resolve_category


def legacy_backfill_audit() -> dict:
    if 'products' not in connection.introspection.table_names():
        return {
            'legacy_rows': 0,
            'source_groups': 0,
            'rows_with_markdown': 0,
            'duplicate_content_rows': 0,
            'paid_embedding_calls': 0,
            'automatic_activation': False,
            'review_policy': 'Legacy products table is not present.',
        }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*),
                   COUNT(DISTINCT COALESCE(NULLIF(source_pdf, ''), '__unknown__')),
                   COUNT(*) FILTER (WHERE COALESCE(markdown_text, '') <> ''),
                   COUNT(*) - COUNT(DISTINCT md5(COALESCE(markdown_text, '')))
            FROM products
            """
        )
        rows, sources, with_markdown, duplicate_content_rows = cursor.fetchone()
    return {
        'legacy_rows': rows,
        'source_groups': sources,
        'rows_with_markdown': with_markdown,
        'duplicate_content_rows': duplicate_content_rows,
        'paid_embedding_calls': 0,
        'automatic_activation': False,
        'review_policy': 'Legacy rows have no reliable structured variants and remain review-gated.',
    }


def _legacy_rows() -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, product_code, product_name, category, source_pdf,
                   page_num, markdown_text, metadata
            FROM products
            ORDER BY COALESCE(NULLIF(source_pdf, ''), '__unknown__'), id
            """
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _stable_slug(source: str) -> str:
    base = slugify(source)[:200] or 'unknown-source'
    suffix = hashlib.sha256(source.encode('utf-8')).hexdigest()[:10]
    return f'legacy-{base}-{suffix}'


def execute_legacy_backfill(*, confirmed_row_count: int) -> dict:
    audit = legacy_backfill_audit()
    if confirmed_row_count != audit['legacy_rows']:
        raise ValueError(
            f"Backfill confirmation mismatch: expected {audit['legacy_rows']}, received {confirmed_row_count}."
        )

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _legacy_rows():
        grouped[str(row.get('source_pdf') or '__unknown__')].append(row)

    totals = {
        'documents': 0,
        'families': 0,
        'variants': 0,
        'chunks': 0,
        'needs_review': 0,
        'embedding_calls': 0,
    }
    for source, rows in grouped.items():
        catalog, _ = Catalog.objects.get_or_create(
            slug=_stable_slug(source),
            defaults={'name': source.replace('_', ' ').strip() or 'Unknown legacy catalog'},
        )
        checksum = hashlib.sha256(f'legacy-source:{source}'.encode('utf-8')).hexdigest()
        document, _ = CatalogDocument.objects.get_or_create(
            checksum_sha256=checksum,
            defaults={
                'catalog': catalog,
                'source_type': CatalogDocument.SourceType.CATALOG,
                'original_filename': source,
                'file': f'legacy/{re.sub(r"[^a-zA-Z0-9_.-]", "_", source)}',
                'version': 1,
                'page_count': max(int(row.get('page_num') or 0) for row in rows),
                'status': CatalogDocument.Status.REVIEW,
                'is_active': False,
            },
        )
        products = []
        for row in rows:
            metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
            description = str(metadata.get('Description') or '')
            features = [line.strip('- ').strip() for line in str(metadata.get('Features') or '').splitlines() if line.strip()]
            products.append({
                'source_key': f"legacy-row:{row['id']}",
                'product_name': row.get('product_name') or f"Unknown Product {row['id']}",
                'product_code': row.get('product_code') or '',
                'raw_category': row.get('category') or '',
                'description': description,
                'features': features,
                'specifications': {'legacy_markdown': str(metadata.get('Specifications') or '')},
                'page_start': int(row.get('page_num') or 0),
                'page_end': int(row.get('page_num') or 0),
                'extraction_confidence': 0.5,
                'children': [],
                '_chunk_text': row.get('markdown_text') or '',
                'legacy_product_id': row['id'],
            })
        summary = persist_assembled_products(document, products)
        totals['documents'] += 1
        for key in ('families', 'variants', 'chunks', 'needs_review', 'embedding_calls'):
            totals[key] += summary[key]
    return {**audit, **totals}


def prepare_legacy_audit() -> dict:
    families = ProductFamily.objects.filter(raw_extraction__has_key='legacy_product_id')
    return {
        'legacy_families': families.count(),
        'already_approved': families.filter(review_status=ProductFamily.ReviewStatus.APPROVED).count(),
        'documents': CatalogDocument.objects.filter(source_schema='legacy-v1').count(),
        'embedding_calls': 0,
    }


def prepare_legacy_for_v2(*, confirmed_family_count: int) -> dict:
    families = list(
        ProductFamily.objects.filter(raw_extraction__has_key='legacy_product_id')
        .select_related('normalized_category')
        .order_by('id')
    )
    if confirmed_family_count != len(families):
        raise ValueError(
            f'Legacy approval confirmation mismatch: expected {len(families)}, '
            f'received {confirmed_family_count}.'
        )

    categories = list(Category.objects.filter(is_active=True).select_related('parent'))
    resolved = 0
    unresolved = 0
    for family in families:
        category = family.normalized_category or resolve_category(
            product_name=family.product_name,
            raw_category=family.raw_category,
            categories=categories,
        )
        family.normalized_category = category
        family.review_status = ProductFamily.ReviewStatus.APPROVED
        if category:
            resolved += 1
        else:
            unresolved += 1
    ProductFamily.objects.bulk_update(families, ('normalized_category', 'review_status'), batch_size=250)

    document_ids = {family.document_id for family in families}
    CatalogDocument.objects.filter(id__in=document_ids).update(
        source_schema='legacy-v1',
        extraction_schema_version=1,
        status=CatalogDocument.Status.INDEXING,
        is_active=False,
        failure_summary='',
    )
    DocumentChunk.objects.filter(document_id__in=document_ids).update(
        extraction_schema_version=1,
        index_status=DocumentChunk.IndexStatus.PENDING,
        index_error='',
    )
    return {
        'approved_families': len(families),
        'normalized_taxonomy_matches': resolved,
        'fallback_raw_categories': unresolved,
        'documents_prepared': len(document_ids),
        'embedding_calls': 0,
    }
