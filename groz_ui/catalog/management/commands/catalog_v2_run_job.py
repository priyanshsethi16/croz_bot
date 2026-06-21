from django.core.management.base import BaseCommand, CommandError

from catalog.services.jobs import run_ingestion_job


class Command(BaseCommand):
    help = 'Run one durable catalog V2 ingestion job.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')
        parser.add_argument('--allow-disabled', action='store_true')

    def handle(self, *args, **options):
        try:
            result = run_ingestion_job(options['job_id'], allow_disabled=options['allow_disabled'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(str(result)))
