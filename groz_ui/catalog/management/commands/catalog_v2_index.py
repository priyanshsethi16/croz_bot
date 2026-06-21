import json

from django.core.management.base import BaseCommand, CommandError

from catalog.models import CatalogDocument
from catalog.services.indexing import index_document, index_dry_run


class Command(BaseCommand):
    help = 'Dry-run or explicitly execute source-aware Qdrant V2 indexing.'

    def add_arguments(self, parser):
        parser.add_argument('document_id')
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-embeddings', type=int)
        parser.add_argument('--allow-disabled', action='store_true')

    def handle(self, *args, **options):
        try:
            document = CatalogDocument.objects.get(pk=options['document_id'])
        except (CatalogDocument.DoesNotExist, ValueError) as exc:
            raise CommandError('Catalog document not found.') from exc

        dry_run = index_dry_run(document)
        self.stdout.write(json.dumps(dry_run.as_dict(), indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: no embedding API calls were made.')
            return
        if options['confirm_embeddings'] is None:
            raise CommandError('--execute requires --confirm-embeddings with the exact dry-run count.')
        try:
            result = index_document(
                document,
                confirmed_embedding_count=options['confirm_embeddings'],
                allow_disabled=options['allow_disabled'],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(json.dumps(result, indent=2)))
