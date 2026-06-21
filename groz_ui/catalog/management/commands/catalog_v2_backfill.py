import json

from django.core.management.base import BaseCommand, CommandError

from catalog.services.backfill import execute_legacy_backfill, legacy_backfill_audit


class Command(BaseCommand):
    help = 'Dry-run or review-gated backfill of the legacy products table into V2.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-rows', type=int)

    def handle(self, *args, **options):
        audit = legacy_backfill_audit()
        self.stdout.write(json.dumps(audit, indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: PostgreSQL V2 and Qdrant were not changed.')
            return
        if options['confirm_rows'] is None:
            raise CommandError('--execute requires --confirm-rows with the exact audit count.')
        try:
            result = execute_legacy_backfill(confirmed_row_count=options['confirm_rows'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(json.dumps(result, indent=2)))
