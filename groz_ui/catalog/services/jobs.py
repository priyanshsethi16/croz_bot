"""Database-backed V2 ingestion worker; safe to invoke outside HTTP requests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog.model_config import subprocess_environment
from catalog.models import CatalogDocument, ExtractionPage, IngestionJob
from catalog.services.structured_ingestion import persist_assembled_products
from catalog.services.manual_ingestion import persist_manual_chunks
from vision_pipeline.manual_chunker import extract_manual_pdf


class IngestionJobError(RuntimeError):
    pass


def claim_next_pending_job() -> str | None:
    with transaction.atomic():
        job = (
            IngestionJob.objects.select_for_update(skip_locked=True)
            .filter(status=IngestionJob.Status.PENDING, cancel_requested=False)
            .order_by('created_at')
            .first()
        )
        if not job:
            return None
        job.status = IngestionJob.Status.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=('status', 'started_at', 'updated_at'))
        return str(job.id)


def _set_job(job_id, **values) -> None:
    values['updated_at'] = timezone.now()
    IngestionJob.objects.filter(pk=job_id).update(**values)


def _record_pages(document: CatalogDocument, products: list[dict]) -> None:
    extracted_pages = {
        int(product.get('original_page_num') or product.get('page_num') or 0)
        for product in products
    }
    by_page: dict[int, list[dict]] = {}
    for product in products:
        page = int(product.get('original_page_num') or product.get('page_num') or 0)
        by_page.setdefault(page, []).append(product)

    for page_number in range(1, document.page_count + 1):
        status = (
            ExtractionPage.Status.EXTRACTED
            if page_number in extracted_pages
            else ExtractionPage.Status.EMPTY
        )
        ExtractionPage.objects.update_or_create(
            document=document,
            page_number=page_number,
            defaults={
                'status': status,
                'attempts': 1,
                'image_path': f'vision_pipeline/data/{document.id}/pages/page_{page_number:03d}.png',
                'extraction_result': by_page.get(page_number, []),
                'error_summary': '',
            },
        )


def run_ingestion_job(job_id: str, *, allow_disabled: bool = False) -> dict:
    if not settings.CATALOG_RAG_V2_INGEST and not allow_disabled:
        raise IngestionJobError('CATALOG_RAG_V2_INGEST is disabled.')

    with transaction.atomic():
        job = IngestionJob.objects.select_for_update().select_related('document').get(pk=job_id)
        if job.cancel_requested or job.status == IngestionJob.Status.CANCELLED:
            job.status = IngestionJob.Status.CANCELLED
            job.completed_at = timezone.now()
            job.save(update_fields=('status', 'completed_at', 'updated_at'))
            return {'status': 'cancelled'}
        if job.status not in (IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING, IngestionJob.Status.FAILED):
            raise IngestionJobError(f'Job {job.id} cannot run from status {job.status}.')
        job.status = IngestionJob.Status.RUNNING
        job.stage = IngestionJob.Stage.RASTERIZE
        job.started_at = job.started_at or timezone.now()
        job.error_summary = ''
        job.save(update_fields=('status', 'stage', 'started_at', 'error_summary', 'updated_at'))
        document = job.document
        document.status = CatalogDocument.Status.EXTRACTING
        document.failure_summary = ''
        document.save(update_fields=('status', 'failure_summary', 'updated_at'))

    artifact_dir = settings.PROJECT_ROOT / 'vision_pipeline' / 'data' / str(document.id)
    assembled_path = artifact_dir / 'assembled_products.json'
    command = [
        sys.executable,
        '-m',
        'vision_pipeline.main',
        '--pdf',
        document.file.path,
        '--document-id',
        str(document.id),
        '--source-pdf',
        document.original_filename,
        '--output-slug',
        str(document.id),
    ]

    try:
        if document.source_type == CatalogDocument.SourceType.MANUAL:
            _set_job(job.id, stage=IngestionJob.Stage.EXTRACT)
            manual_chunks = extract_manual_pdf(document.file.path)
            summary = persist_manual_chunks(document, manual_chunks)
            pages_with_text = {chunk.page_start for chunk in manual_chunks}
            for page_number in range(1, document.page_count + 1):
                ExtractionPage.objects.update_or_create(
                    document=document,
                    page_number=page_number,
                    defaults={
                        'status': (
                            ExtractionPage.Status.EXTRACTED
                            if page_number in pages_with_text
                            else ExtractionPage.Status.EMPTY
                        ),
                        'attempts': 1,
                        'extraction_result': [
                            chunk.as_dict() for chunk in manual_chunks if chunk.page_start == page_number
                        ],
                        'error_summary': '',
                    },
                )
            _set_job(
                job.id,
                stage=IngestionJob.Stage.COMPLETE,
                status=IngestionJob.Status.SUCCEEDED,
                completed_units=document.page_count,
                completed_at=timezone.now(),
                error_summary='',
            )
            return {'status': 'succeeded', **summary, 'source_type': 'manual'}

        _set_job(job.id, stage=IngestionJob.Stage.EXTRACT)
        result = subprocess.run(
            command,
            cwd=str(settings.PROJECT_ROOT),
            env=subprocess_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=None,
        )
        if result.returncode != 0:
            details = (result.stdout or '')[-4000:]
            raise IngestionJobError(f'Vision pipeline failed with exit code {result.returncode}:\n{details}')
        if not assembled_path.exists():
            raise IngestionJobError('Vision pipeline completed without assembled_products.json.')

        assembled = json.loads(assembled_path.read_text(encoding='utf-8'))
        if not isinstance(assembled, list):
            raise IngestionJobError('Assembled product artifact is not a JSON array.')
        products_path = artifact_dir / 'products.json'
        page_products = json.loads(products_path.read_text(encoding='utf-8')) if products_path.exists() else []

        _set_job(job.id, stage=IngestionJob.Stage.ASSEMBLE)
        _record_pages(document, page_products)
        _set_job(job.id, stage=IngestionJob.Stage.VALIDATE, completed_units=document.page_count)
        summary = persist_assembled_products(document, assembled)

        _set_job(
            job.id,
            stage=IngestionJob.Stage.COMPLETE,
            status=IngestionJob.Status.SUCCEEDED,
            completed_at=timezone.now(),
            error_summary='',
        )
        return {'status': 'succeeded', **summary, 'artifact_dir': str(artifact_dir)}
    except Exception as exc:
        message = str(exc)[-4000:]
        _set_job(
            job.id,
            status=IngestionJob.Status.FAILED,
            error_summary=message,
            completed_at=timezone.now(),
        )
        CatalogDocument.objects.filter(pk=document.pk).update(
            status=CatalogDocument.Status.FAILED,
            failure_summary=message,
            updated_at=timezone.now(),
        )
        raise
