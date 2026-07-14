from django.core.management.base import BaseCommand, CommandError

from catalog.services.jobs import run_ingestion_job


class Command(BaseCommand):
    help = 'Run one durable catalog V2 ingestion job.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')

    def handle(self, *args, **options):
        try:
            result = run_ingestion_job(options['job_id'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(str(result)))
