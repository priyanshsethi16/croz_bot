import json

from django.core.management.base import BaseCommand, CommandError

from catalog.models import CatalogDocument, DocumentChunk, ProductFamily
from catalog.services.indexing import index_document, index_dry_run


class Command(BaseCommand):
    help = 'Reconcile and activate vector-reused V2 documents with zero embeddings.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-documents', type=int)

    def handle(self, *args, **options):
        documents = list(
            CatalogDocument.objects.filter(
                source_schema='legacy-v1',
                status__in=(CatalogDocument.Status.INDEXING, CatalogDocument.Status.READY),
            ).order_by('id')
        )
        pending_embeddings = DocumentChunk.objects.filter(
            document__in=documents,
            index_status__in=(DocumentChunk.IndexStatus.PENDING, DocumentChunk.IndexStatus.STALE),
        ).count()
        review_blocked = ProductFamily.objects.filter(
            document__in=documents,
        ).exclude(review_status=ProductFamily.ReviewStatus.APPROVED).count()
        audit = {
            'documents': len(documents),
            'already_ready': sum(document.status == CatalogDocument.Status.READY for document in documents),
            'pending_embeddings': pending_embeddings,
            'review_blocked': review_blocked,
        }
        self.stdout.write(json.dumps(audit, indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: no document/payload activation was changed.')
            return
        if options['confirm_documents'] != len(documents):
            raise CommandError(f'--confirm-documents must equal {len(documents)}.')
        if pending_embeddings:
            raise CommandError(f'{pending_embeddings} chunks still require embeddings.')
        if review_blocked:
            raise CommandError(f'{review_blocked} products are not approved.')

        activated = 0
        failures = []
        for document in documents:
            if document.status == CatalogDocument.Status.READY and document.is_active:
                activated += 1
                continue
            try:
                dry_run = index_dry_run(document)
                if dry_run.pending_embeddings or dry_run.review_blocked or dry_run.failed_chunks:
                    raise ValueError(f'Document is not activation-ready: {dry_run.as_dict()}')
                index_document(
                    document,
                    confirmed_embedding_count=0,
                    allow_disabled=True,
                )
                activated += 1
                self.stdout.write(f'Activated {document.id} ({activated}/{len(documents)})')
            except Exception as exc:
                failures.append({'document_id': str(document.id), 'error': str(exc)})

        result = {'activated': activated, 'failures': failures, 'embedding_calls': 0}
        self.stdout.write(json.dumps(result, indent=2))
        if failures:
            raise CommandError(f'{len(failures)} document(s) failed activation.')
