"""Source-aware Qdrant V2 indexing with explicit embedding-cost confirmation."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from langchain_core.documents import Document
from qdrant_client import models

from catalog.model_config import get_runtime_config
from catalog.models import CatalogDocument, DocumentChunk, ProductFamily
from rag_pipeline.providers import COLLECTION, build_qdrant_client, build_vector_store, ensure_collection


@dataclass(frozen=True)
class IndexDryRun:
    document_id: str
    total_chunks: int
    pending_embeddings: int
    indexed_chunks: int
    review_blocked: int
    failed_chunks: int

    def as_dict(self) -> dict:
        return asdict(self)


def index_dry_run(document: CatalogDocument) -> IndexDryRun:
    # Import Django Q locally to keep qdrant model names unambiguous.
    from django.db.models import Q

    base = DocumentChunk.objects.filter(document=document)
    approved = base
    blocked = base.none()
    return IndexDryRun(
        document_id=str(document.id),
        total_chunks=base.count(),
        pending_embeddings=approved.filter(index_status__in=(
            DocumentChunk.IndexStatus.PENDING,
            DocumentChunk.IndexStatus.STALE,
            DocumentChunk.IndexStatus.FAILED,
        )).count(),
        indexed_chunks=base.filter(index_status=DocumentChunk.IndexStatus.INDEXED).count(),
        review_blocked=blocked.count(),
        failed_chunks=base.filter(index_status=DocumentChunk.IndexStatus.FAILED).count(),
    )


def _metadata(chunk: DocumentChunk) -> dict:
    family = chunk.family
    leaf_category = family.normalized_category if family else None
    root_category = leaf_category
    while root_category and root_category.parent_id:
        root_category = root_category.parent
    fallback_category = slugify(family.raw_category)[:160] if family and family.raw_category else 'uncategorized'
    normalized_category = root_category.slug if root_category else fallback_category
    normalized_subcategory = (
        leaf_category.slug
        if leaf_category and root_category and leaf_category.id != root_category.id
        else ''
    )
    return {
        'schema_version': 2,
        'source_schema': chunk.document.source_schema,
        'catalog_id': str(chunk.document.catalog_id),
        'document_id': str(chunk.document_id),
        'document_version': chunk.document.version,
        'product_family_id': str(chunk.family_id or ''),
        'product_variant_id': str(chunk.variant_id or ''),
        'product_name': family.product_name if family else '',
        'product_code': family.product_code if family else '',
        'normalized_category': normalized_category,
        'normalized_subcategory': normalized_subcategory,
        'raw_category': family.raw_category if family else '',
        'source_type': chunk.document.source_type,
        'source_pdf': chunk.document.original_filename,
        'page_start': chunk.page_start,
        'page_end': chunk.page_end,
        'language': 'en',
        'is_active': bool(chunk.document.is_active),
        'extraction_schema_version': chunk.extraction_schema_version,
        'chunk_hash': chunk.content_hash,
        'chunk_type': chunk.chunk_type,
        'linked_product_codes': chunk.linked_product_codes,
    }


def reconcile_document(document: CatalogDocument) -> dict:
    client = build_qdrant_client()
    expected = {
        str(point_id)
        for point_id in DocumentChunk.objects.filter(
            document=document,
            index_status=DocumentChunk.IndexStatus.INDEXED,
        ).exclude(qdrant_point_id=None).values_list('qdrant_point_id', flat=True)
    }
    if not client.collection_exists(COLLECTION):
        return {
            'document_id': str(document.id),
            'expected': len(expected),
            'actual': 0,
            'missing': sorted(expected),
            'unexpected': [],
        }

    actual: set[str] = set()
    offset = None
    query_filter = models.Filter(must=[
        models.FieldCondition(
            key='metadata.document_id',
            match=models.MatchValue(value=str(document.id)),
        )
    ])
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        actual.update(str(point.id) for point in points)
        if offset is None:
            break

    return {
        'document_id': str(document.id),
        'expected': len(expected),
        'actual': len(actual),
        'missing': sorted(expected - actual),
        'unexpected': sorted(actual - expected),
    }


def _set_document_active_payload(document: CatalogDocument, *, active: bool) -> None:
    client = build_qdrant_client()
    document_filter = models.Filter(must=[
        models.FieldCondition(
            key='metadata.document_id',
            match=models.MatchValue(value=str(document.id)),
        )
    ])
    client.set_payload(
        collection_name=COLLECTION,
        payload={'is_active': active},
        points=models.FilterSelector(filter=document_filter),
        key='metadata',
    )


def _activate_document_payload(document: CatalogDocument, previous_active_ids: list) -> None:
    for old_id in previous_active_ids:
        old = CatalogDocument(id=old_id, catalog_id=document.catalog_id)
        _set_document_active_payload(old, active=False)
    _set_document_active_payload(document, active=True)


def index_document(
    document: CatalogDocument,
    *,
    confirmed_embedding_count: int,
    batch_size: int = 64,
) -> dict:
    """Embed/index exactly the approved pending count and atomically activate on success."""
    if document.status == CatalogDocument.Status.ARCHIVED:
        raise ValueError('Archived documents cannot be indexed.')

    from django.db.models import Q

    dry_run = index_dry_run(document)
    if dry_run.total_chunks == 0:
        raise ValueError('Document has no validated chunks to index.')
    if confirmed_embedding_count != dry_run.pending_embeddings:
        raise ValueError(
            f'Embedding confirmation mismatch: expected {dry_run.pending_embeddings}, '
            f'received {confirmed_embedding_count}. Run the dry-run again.'
        )
    if dry_run.review_blocked:
        raise ValueError(f'{dry_run.review_blocked} chunks are blocked by product review.')

    pending = list(
        DocumentChunk.objects.select_related(
            'document', 'family', 'family__normalized_category',
            'family__normalized_category__parent', 'variant'
        ).filter(document=document).filter(
            index_status__in=(
                DocumentChunk.IndexStatus.PENDING,
                DocumentChunk.IndexStatus.STALE,
                DocumentChunk.IndexStatus.FAILED,
            )
        )
    )

    ensure_collection()
    runtime = get_runtime_config()
    vector_store = build_vector_store() if pending else None
    CatalogDocument.objects.filter(pk=document.pk).update(
        status=CatalogDocument.Status.INDEXING,
        updated_at=timezone.now(),
    )

    indexed = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        documents = [Document(page_content=chunk.text, metadata=_metadata(chunk)) for chunk in batch]
        ids = [str(chunk.qdrant_point_id or chunk.id) for chunk in batch]
        try:
            vector_store.add_documents(documents=documents, ids=ids)
        except Exception as exc:
            DocumentChunk.objects.filter(id__in=[chunk.id for chunk in batch]).update(
                index_status=DocumentChunk.IndexStatus.FAILED,
                index_error=str(exc)[-2000:],
            )
            CatalogDocument.objects.filter(pk=document.pk).update(
                status=CatalogDocument.Status.FAILED,
                failure_summary=str(exc)[-2000:],
                updated_at=timezone.now(),
            )
            raise
        DocumentChunk.objects.filter(id__in=[chunk.id for chunk in batch]).update(
            index_status=DocumentChunk.IndexStatus.INDEXED,
            index_error='',
        )
        indexed += len(batch)

    reconciliation = reconcile_document(document)
    if reconciliation['missing'] or reconciliation['unexpected']:
        raise RuntimeError(f'Qdrant reconciliation failed: {reconciliation}')

    previous_active_ids = list(
        CatalogDocument.objects.filter(catalog=document.catalog, is_active=True)
        .exclude(pk=document.pk)
        .values_list('id', flat=True)
    )
    _activate_document_payload(document, previous_active_ids)
    try:
        with transaction.atomic():
            locked = CatalogDocument.objects.select_for_update().get(pk=document.pk)
            CatalogDocument.objects.filter(catalog=locked.catalog, is_active=True).exclude(pk=locked.pk).update(
                is_active=False,
                updated_at=timezone.now(),
            )
            locked.is_active = True
            locked.status = CatalogDocument.Status.READY
            locked.failure_summary = ''
            locked.save(update_fields=('is_active', 'status', 'failure_summary', 'updated_at'))
    except Exception:
        _set_document_active_payload(document, active=False)
        for old_id in previous_active_ids:
            old = CatalogDocument(id=old_id, catalog_id=document.catalog_id)
            _set_document_active_payload(old, active=True)
        raise
    return {'indexed': indexed, 'dry_run': dry_run.as_dict(), 'reconciliation': reconciliation}


def deactivate_document_points(document: CatalogDocument) -> None:
    client = build_qdrant_client()
    if not client.collection_exists(COLLECTION):
        return
    query_filter = models.Filter(must=[
        models.FieldCondition(
            key='metadata.document_id',
            match=models.MatchValue(value=str(document.id)),
        )
    ])
    client.set_payload(
        collection_name=COLLECTION,
        payload={'is_active': False},
        points=models.FilterSelector(filter=query_filter),
        key='metadata',
    )


def refresh_chunk_payloads(chunks) -> int:
    """Refresh V2 metadata only; preserve page content and both vectors."""
    client = build_qdrant_client()
    if not client.collection_exists(COLLECTION):
        return 0
    
    refreshed = 0
    for chunk in chunks.select_related(
        'document', 'family', 'family__normalized_category',
        'family__normalized_category__parent', 'variant',
    ):
        try:
            client.set_payload(
                collection_name=COLLECTION,
                payload=_metadata(chunk),
                points=[str(chunk.qdrant_point_id or chunk.id)],
                key='metadata',
            )
            refreshed += 1
        except Exception as exc:
            # Silently skip chunks that don't exist in Qdrant yet (not indexed)
            # This can happen when chunks are marked as indexed in DB but haven't been embedded
            import logging
            logging.warning(
                f'Could not refresh payload for chunk {chunk.id} (point {chunk.qdrant_point_id or chunk.id}): {exc}'
            )
            # Mark chunk as STALE so it gets re-indexed later
            if chunk.index_status == DocumentChunk.IndexStatus.INDEXED:
                DocumentChunk.objects.filter(id=chunk.id).update(
                    index_status=DocumentChunk.IndexStatus.STALE
                )
    return refreshed
