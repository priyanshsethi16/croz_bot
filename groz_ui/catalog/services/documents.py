"""Safe catalog-document creation and lifecycle helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

from django.db import transaction
from django.db.models import Max
from django.utils.text import slugify
from pypdf import PdfReader

from catalog.models import Catalog, CatalogDocument, IngestionJob


class DuplicateDocumentError(ValueError):
    def __init__(self, document: CatalogDocument):
        self.document = document
        super().__init__(f'This PDF is already registered as document {document.id}.')


def _checksum(uploaded_file: BinaryIO) -> str:
    uploaded_file.seek(0)
    digest = hashlib.sha256()
    chunks = getattr(uploaded_file, 'chunks', None)
    iterator = chunks() if callable(chunks) else iter(lambda: uploaded_file.read(1024 * 1024), b'')
    for block in iterator:
        digest.update(block)
    uploaded_file.seek(0)
    return digest.hexdigest()


def _assert_pdf(uploaded_file: BinaryIO, original_filename: str) -> None:
    if Path(original_filename).suffix.lower() != '.pdf':
        raise ValueError('Only PDF files can be uploaded.')
    uploaded_file.seek(0)
    header = uploaded_file.read(5)
    uploaded_file.seek(0)
    if header != b'%PDF-':
        raise ValueError('The uploaded file does not have a valid PDF header.')


@transaction.atomic
def create_catalog_document(
    *,
    uploaded_file,
    user=None,
    catalog: Catalog | None = None,
    catalog_name: str = '',
    source_type: str = CatalogDocument.SourceType.CATALOG,
) -> tuple[CatalogDocument, IngestionJob]:
    """Register an immutable PDF and pending job without running the VLM."""
    original_filename = Path(uploaded_file.name).name
    _assert_pdf(uploaded_file, original_filename)
    checksum = _checksum(uploaded_file)
    existing = CatalogDocument.objects.filter(checksum_sha256=checksum).first()
    if existing:
        raise DuplicateDocumentError(existing)

    if source_type not in CatalogDocument.SourceType.values:
        raise ValueError('source_type must be catalog or manual.')

    if catalog is None:
        name = catalog_name.strip() or Path(original_filename).stem.replace('_', ' ')
        base_slug = slugify(name)[:220] or 'catalog'
        catalog = Catalog.objects.filter(slug=base_slug).first()
        if catalog is None:
            catalog = Catalog.objects.create(name=name, slug=base_slug)

    version = (catalog.documents.aggregate(value=Max('version'))['value'] or 0) + 1
    document = CatalogDocument(
        catalog=catalog,
        source_type=source_type,
        original_filename=original_filename,
        checksum_sha256=checksum,
        version=version,
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    document.file.save(original_filename, uploaded_file, save=False)
    try:
        document.page_count = len(PdfReader(document.file.path).pages)
        document.save()
    except Exception:
        document.file.delete(save=False)
        raise

    job = IngestionJob.objects.create(
        document=document,
        stage=IngestionJob.Stage.UPLOAD,
        status=IngestionJob.Status.PENDING,
        total_units=document.page_count,
    )
    return document, job


@transaction.atomic
def archive_document(document: CatalogDocument) -> CatalogDocument:
    document = CatalogDocument.objects.select_for_update().get(pk=document.pk)
    from catalog.services.indexing import deactivate_document_points

    deactivate_document_points(document)
    document.is_active = False
    document.status = CatalogDocument.Status.ARCHIVED
    document.save(update_fields=('is_active', 'status', 'updated_at'))
    document.jobs.filter(status__in=(IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING)).update(
        cancel_requested=True,
    )
    return document
