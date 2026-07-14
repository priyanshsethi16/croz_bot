import time

from django.core.management.base import BaseCommand, CommandError

from catalog.services.jobs import claim_next_pending_job, run_ingestion_job


class Command(BaseCommand):
    help = 'Run the database-backed catalog V2 worker.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--poll-seconds', type=float, default=2.0)

    def handle(self, *args, **options):
        while True:
            job_id = claim_next_pending_job()
            if job_id:
                try:
                    run_ingestion_job(job_id)
                    self.stdout.write(self.style.SUCCESS(f'Completed job {job_id}'))
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f'Job {job_id} failed: {exc}'))
            elif options['once']:
                self.stdout.write('No pending V2 ingestion jobs.')
                return

            if options['once']:
                return
            if options['poll_seconds'] <= 0:
                raise CommandError('--poll-seconds must be greater than zero.')
            time.sleep(options['poll_seconds'])
