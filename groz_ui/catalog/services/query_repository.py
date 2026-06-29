"""Parameterized PostgreSQL repositories for deterministic V2 catalog queries."""

from __future__ import annotations

import json
import uuid
import re
from dataclasses import dataclass

from django.db.models import Prefetch, Q

from catalog.models import CatalogDocument, Category, DocumentChunk, ProductFamily, ProductVariant
from catalog.services.taxonomy import descendant_category_ids, normalize_taxonomy_text
from rag_pipeline.planner import QueryScope


@dataclass(frozen=True)
class ProductPage:
    products: list[dict]
    total: int
    page: int
    page_size: int

    @property
    def complete(self) -> bool:
        return self.page * self.page_size >= self.total


def _valid_uuids(values: list[str]) -> list[uuid.UUID]:
    result = []
    for value in values:
        try:
            result.append(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def active_documents(scope: QueryScope):
    queryset = CatalogDocument.objects.filter(is_active=True, status=CatalogDocument.Status.READY)
    catalog_ids = _valid_uuids(scope.catalog_ids)
    document_ids = _valid_uuids(scope.document_ids)
    if catalog_ids:
        queryset = queryset.filter(catalog_id__in=catalog_ids)
    if document_ids:
        queryset = queryset.filter(id__in=document_ids)
    if scope.source_types:
        queryset = queryset.filter(source_type__in=scope.source_types)
    return queryset


def resolve_category_term(value: str) -> Category | None:
    needle = normalize_taxonomy_text(value)
    if not needle:
        return None
    for category in Category.objects.filter(is_active=True).select_related('parent'):
        terms = {category.name, category.slug.replace('-', ' '), *(category.aliases or [])}
        if needle in {normalize_taxonomy_text(term) for term in terms}:
            return category
    return None


def _family_queryset(scope: QueryScope):
    documents = active_documents(scope)
    return (
        ProductFamily.objects.filter(
            document__in=documents,
        )
        .exclude(review_status=ProductFamily.ReviewStatus.REJECTED)
        .select_related('document', 'document__catalog', 'normalized_category')
        .prefetch_related(Prefetch('variants', queryset=ProductVariant.objects.order_by('product_code', 'order_number', 'name')))
    )


def _serialize_family(family: ProductFamily) -> dict:
    return {
        'family_id': str(family.id),
        'product_name': family.product_name,
        'product_code': family.product_code,
        'raw_category': family.raw_category,
        'aliases': family.aliases or [],
        'category': family.normalized_category.name if family.normalized_category else family.raw_category,
        'category_slug': family.normalized_category.slug if family.normalized_category else '',
        'description': family.description,
        'features': family.features,
        'utilities': family.utilities,
        'materials': family.materials,
        'specifications': family.specifications,
        'variants': [
            {
                'variant_id': str(variant.id),
                'product_code': variant.product_code,
                'order_number': variant.order_number,
                'name': variant.name,
                'size': variant.size,
                'unit': variant.unit,
                'specifications': variant.specifications,
                'ordering_data': variant.ordering_data,
            }
            for variant in family.variants.all()
        ],
        'source': {
            'catalog_id': str(family.document.catalog_id),
            'catalog': family.document.catalog.name,
            'document_id': str(family.document_id),
            'version': family.document.version,
            'source_pdf': family.document.original_filename,
            'page_start': family.page_start,
            'page_end': family.page_end,
        },
    }


def _product_identity(product: dict) -> str:
    code = re.sub(r'\s+', '', str(product.get('product_code') or '').upper())
    name = re.sub(r'[^a-z0-9]+', ' ', str(product.get('product_name') or '').lower()).strip()
    if code:
        # A code can legitimately identify multiple differently described
        # families across catalogs. Collapse only the same named product so
        # exact lookups do not lose complementary specifications.
        return f'code:{code}|name:{name}'
    return f'name:{name}'


def _product_richness(product: dict) -> tuple:
    return (
        len(product.get('variants') or []),
        len(json.dumps(product.get('specifications') or {}, ensure_ascii=False)),
        len(product.get('description') or ''),
        len(product.get('features') or []),
    )


def _deduplicate_products(products: list[dict]) -> list[dict]:
    """Keep the richest source record for an equivalent code-and-name pair."""
    selected: dict[str, dict] = {}
    for product in products:
        key = _product_identity(product)
        current = selected.get(key)
        if current is None or _product_richness(product) > _product_richness(current):
            selected[key] = product
    return sorted(
        selected.values(),
        key=lambda product: (product['product_name'].lower(), product['product_code'].lower()),
    )


def list_products(
    *,
    category_value: str = '',
    scope: QueryScope,
    page: int = 1,
    page_size: int = 50,
) -> ProductPage:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    queryset = _family_queryset(scope)
    if category_value:
        category = resolve_category_term(category_value)
        if category is None:
            return ProductPage(products=[], total=0, page=page, page_size=page_size)
        queryset = queryset.filter(normalized_category_id__in=descendant_category_ids(category))

    queryset = queryset.order_by('product_name', 'product_code', 'id')
    unique_products = _deduplicate_products([_serialize_family(family) for family in queryset])
    total = len(unique_products)
    start = (page - 1) * page_size
    products = unique_products[start:start + page_size]
    return ProductPage(products=products, total=total, page=page, page_size=page_size)


def find_by_product_codes(codes: list[str], *, scope: QueryScope) -> list[dict]:
    normalized_codes = [code.strip().upper() for code in codes if code.strip()]
    if not normalized_codes:
        return []
    code_filter = Q()
    for code in normalized_codes:
        code_filter |= (
            Q(product_code__iexact=code)
            | Q(variants__product_code__iexact=code)
            | Q(variants__order_number__iexact=code)
        )
    queryset = _family_queryset(scope).filter(code_filter)
    products = [_serialize_family(family) for family in queryset.distinct().order_by('product_name')]
    return _deduplicate_products(products)


def resolve_product_code_tokens(candidates: list[str], *, scope: QueryScope) -> list[str]:
    """Return only user tokens that resolve to real family/variant/order codes."""
    normalized_candidates = [candidate.strip().upper() for candidate in candidates if candidate.strip()]
    products = find_by_product_codes(normalized_candidates, scope=scope)
    known_codes: set[str] = set()
    for product in products:
        family_code = str(product.get('product_code') or '').strip().upper()
        if family_code:
            known_codes.add(family_code)
            known_codes.update(
                part.strip()
                for part in re.split(r'\s+/\s+', family_code)
                if part.strip()
            )
        for variant in product.get('variants') or []:
            for value in (variant.get('product_code'), variant.get('order_number')):
                if value:
                    known_codes.add(str(value).strip().upper())
    return list(dict.fromkeys(
        candidate for candidate in normalized_candidates if candidate in known_codes
    ))


def list_products_filtered(
    *,
    categories: list[str],
    materials: list[str],
    scope: QueryScope,
    limit: int = 100,
) -> list[dict]:
    """Return structured inventory matching normalized categories and material terms."""
    category_values = [value.strip() for value in categories if value.strip()]
    if category_values:
        candidates = []
        for category in category_values:
            candidates.extend(list_products(
                category_value=category,
                scope=scope,
                page=1,
                page_size=100,
            ).products)
        candidates = _deduplicate_products(candidates)
    else:
        candidates = list_products(scope=scope, page=1, page_size=100).products

    needles = [normalize_taxonomy_text(value) for value in materials if normalize_taxonomy_text(value)]
    if needles:
        filtered = []
        for product in candidates:
            haystack = normalize_taxonomy_text(' '.join([
                str(product.get('product_name') or ''),
                str(product.get('product_code') or ''),
                str(product.get('raw_category') or ''),
                str(product.get('category') or ''),
                ' '.join(product.get('aliases') or []),
                str(product.get('description') or ''),
                json.dumps(product.get('materials') or {}, ensure_ascii=False),
            ]))
            if any(needle in haystack for needle in needles):
                filtered.append(product)
        candidates = filtered
    return candidates[:max(1, min(limit, 100))]


def source_chunks_for_products(products: list[dict]) -> list[dict]:
    """Load original reviewed Markdown for deterministic inventory families."""
    family_ids = _valid_uuids([product.get('family_id') for product in products])
    if not family_ids:
        return []
    chunks = DocumentChunk.objects.filter(
        family_id__in=family_ids,
        index_status=DocumentChunk.IndexStatus.INDEXED,
    ).select_related('family', 'document', 'document__catalog')
    return [
        {
            'text': chunk.text,
            'metadata': {
                'chunk_hash': chunk.content_hash,
                'product_family_id': str(chunk.family_id),
                'product_name': chunk.family.product_name if chunk.family else '',
                'product_code': chunk.family.product_code if chunk.family else '',
                'catalog_id': str(chunk.document.catalog_id),
                'catalog': chunk.document.catalog.name,
                'document_id': str(chunk.document_id),
                'source_pdf': chunk.document.original_filename,
                'page_start': chunk.page_start,
                'page_end': chunk.page_end,
            },
            'score': 1.0,
        }
        for chunk in chunks
    ]


def product_to_chunk(product: dict, *, score: float = 1.0) -> dict:
    source = product['source']
    text = (
        f"# {product['product_name']}\n\n"
        f"Product Code: {product['product_code']}\n\n"
        f"Category: {product['category']}\n\n"
        f"Aliases: {json.dumps(product.get('aliases') or [], ensure_ascii=False)}\n\n"
        f"Description: {product['description']}\n\n"
        f"Features: {json.dumps(product['features'], ensure_ascii=False)}\n\n"
        f"Specifications: {json.dumps(product['specifications'], ensure_ascii=False)}\n\n"
        f"Variants: {json.dumps(product['variants'], ensure_ascii=False)}"
    )
    return {
        'text': text,
        'metadata': {
            'product_family_id': product['family_id'],
            'product_name': product['product_name'],
            'product_code': product['product_code'],
            'normalized_category': product['category_slug'],
            **source,
        },
        'score': score,
    }
