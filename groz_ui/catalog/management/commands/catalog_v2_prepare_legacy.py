import json

from django.core.management.base import BaseCommand, CommandError

from catalog.services.backfill import prepare_legacy_audit, prepare_legacy_for_v2


class Command(BaseCommand):
    help = 'Approve confirmed legacy chunks and derive V2 metadata without embeddings.'

    def add_arguments(self, parser):
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-families', type=int)

    def handle(self, *args, **options):
        audit = prepare_legacy_audit()
        self.stdout.write(json.dumps(audit, indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: no review/category state was changed.')
            return
        if options['confirm_families'] is None:
            raise CommandError('--execute requires --confirm-families with the exact audit count.')
        try:
            result = prepare_legacy_for_v2(confirmed_family_count=options['confirm_families'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(json.dumps(result, indent=2)))
