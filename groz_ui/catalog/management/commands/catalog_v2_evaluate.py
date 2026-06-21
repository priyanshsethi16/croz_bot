import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from catalog.model_config import get_runtime_config
from catalog.models import CatalogDocument
from catalog.services.query_engine import CatalogQueryEngine


class Command(BaseCommand):
    help = 'Dry-run or execute the reviewed English V2 query evaluation set.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset',
            default=str(settings.PROJECT_ROOT / 'tests' / 'fixtures' / 'catalog_rag_eval.json'),
        )
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-queries', type=int)

    def handle(self, *args, **options):
        path = Path(options['dataset'])
        try:
            cases = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Could not read evaluation dataset: {exc}') from exc
        if not isinstance(cases, list):
            raise CommandError('Evaluation dataset must be a JSON array.')

        active_documents = CatalogDocument.objects.filter(
            is_active=True,
            status=CatalogDocument.Status.READY,
        ).count()
        audit = {
            'queries': len(cases),
            'active_v2_documents': active_documents,
            'will_call_query_embeddings_or_chat': bool(options['execute']),
        }
        self.stdout.write(json.dumps(audit, indent=2))
        if not options['execute']:
            self.stdout.write('Dry-run only: no model API calls were made.')
            return
        if options['confirm_queries'] != len(cases):
            raise CommandError(f'--confirm-queries must equal the current dataset size ({len(cases)}).')
        if active_documents == 0:
            raise CommandError('No reviewed, indexed, active V2 documents are available for evaluation.')

        engine = CatalogQueryEngine(get_runtime_config())
        reports = []
        for case in cases:
            execution = engine.execute(str(case['query']))
            source_codes = {source['code'] for source in execution.sources if source.get('code')}
            expected_codes = set(case.get('expected_codes') or [])
            reports.append({
                'level': case.get('level'),
                'query': case['query'],
                'intent': execution.plan.intent.value,
                'intent_pass': execution.plan.intent.value == case.get('expected_intent'),
                'expected_codes_found': sorted(expected_codes & source_codes),
                'missing_expected_codes': sorted(expected_codes - source_codes),
                'complete_result': execution.complete_result,
                'total_results': execution.total_results,
            })
        self.stdout.write(json.dumps(reports, indent=2))
