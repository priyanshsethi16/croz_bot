import json

from django.core.management.base import BaseCommand, CommandError

from catalog.models import CatalogDocument, DocumentChunk
from rag_pipeline.providers import COLLECTION, COLLECTION_ALIAS, activate_collection_alias


class Command(BaseCommand):
    help = 'Audit or activate the stable Qdrant V2 alias after evaluation passes.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-ready-documents', type=int)

    def handle(self, *args, **options):
        ready = CatalogDocument.objects.filter(
            status=CatalogDocument.Status.READY,
            is_active=True,
        ).count()
        failed_chunks = DocumentChunk.objects.filter(index_status=DocumentChunk.IndexStatus.FAILED).count()
        audit = {
            'ready_active_documents': ready,
            'failed_chunks': failed_chunks,
            'collection': COLLECTION,
            'alias': COLLECTION_ALIAS,
        }
        self.stdout.write(json.dumps(audit, indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: alias was not changed.')
            return
        if ready == 0:
            raise CommandError('No ready active V2 documents are available.')
        if failed_chunks:
            raise CommandError(f'{failed_chunks} failed V2 chunks must be resolved before activation.')
        if options['confirm_ready_documents'] != ready:
            raise CommandError(f'--confirm-ready-documents must equal {ready}.')
        activate_collection_alias()
        self.stdout.write(self.style.SUCCESS(f'Alias {COLLECTION_ALIAS} now points to {COLLECTION}.'))
