import json

from django.core.management.base import BaseCommand, CommandError

from catalog.models import CatalogDocument
from catalog.services.indexing import reconcile_document


class Command(BaseCommand):
    help = 'Compare V2 PostgreSQL indexed chunks with source-aware Qdrant points.'

    def add_arguments(self, parser):
        parser.add_argument('document_id')

    def handle(self, *args, **options):
        try:
            document = CatalogDocument.objects.get(pk=options['document_id'])
        except (CatalogDocument.DoesNotExist, ValueError) as exc:
            raise CommandError('Catalog document not found.') from exc
        report = reconcile_document(document)
        self.stdout.write(json.dumps(report, indent=2))
        if report['missing'] or report['unexpected']:
            raise CommandError('Reconciliation mismatch detected.')
