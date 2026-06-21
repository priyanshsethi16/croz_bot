"""Persist manual sections as reviewable, source-aware pending chunks."""

from __future__ import annotations

import hashlib
import uuid

from django.db import transaction

from catalog.models import CatalogDocument, DocumentChunk
from vision_pipeline.manual_chunker import ManualChunkData


@transaction.atomic
def persist_manual_chunks(document: CatalogDocument, chunks: list[ManualChunkData]) -> dict:
    document = CatalogDocument.objects.select_for_update().get(pk=document.pk)
    if document.source_type != CatalogDocument.SourceType.MANUAL:
        raise ValueError('Manual chunks can only be attached to source_type=manual documents.')
    if document.is_active and document.chunks.exists():
        raise ValueError('An active manual document is immutable; create a new version.')
    document.chunks.all().delete()

    for chunk in chunks:
        text = f'# {chunk.title}\n\n{chunk.text}'
        content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        point_id = uuid.uuid5(document.id, f'manual:{chunk.ordinal}:{content_hash}')
        DocumentChunk.objects.create(
            id=point_id,
            document=document,
            chunk_type=DocumentChunk.ChunkType.MANUAL_SECTION,
            text=text,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            content_hash=content_hash,
            ordinal=chunk.ordinal,
            qdrant_point_id=point_id,
            index_status=DocumentChunk.IndexStatus.PENDING,
            extraction_schema_version=document.extraction_schema_version,
            linked_product_codes=chunk.linked_product_codes,
        )
    document.status = CatalogDocument.Status.INDEXING
    document.failure_summary = ''
    document.save(update_fields=('status', 'failure_summary', 'updated_at'))
    return {'chunks': len(chunks), 'embedding_calls': 0}
