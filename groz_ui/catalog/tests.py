import json
import hashlib
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from pypdf import PdfWriter

from .model_config import SECRET_GEMINI, SECRET_OPENAI, get_secret
from .models import (
    ApiKey,
    Catalog,
    CatalogDocument,
    Category,
    DocumentChunk,
    ExtractionPage,
    IngestionJob,
    ModelConfiguration,
    ProductFamily,
    ProductVariant,
)
from .services.structured_ingestion import persist_assembled_products
from .services.jobs import run_ingestion_job
from .services.indexing import _metadata, index_document, index_dry_run
from .services.query_engine import (
    CatalogQueryEngine,
    _append_planned_result_status,
    _missing_answer_constraints,
    _remove_unverified_variant_code_lines,
    _strip_model_constraint_selections,
    _strip_empty_markdown_headings,
    _render_constraint_matches,
    _render_verified_variant_table,
    _render_exhaustive_answer,
)
from .services.query_repository import list_products
from .services.query_repository import find_by_product_codes
from .services.query_repository import resolve_product_code_tokens
from .services.manual_ingestion import persist_manual_chunks
from .services.taxonomy import resolve_category
from rag_pipeline.planner import QueryIntent, QueryPlan, QueryRoute, QueryScope, QueryTask
from vision_pipeline.manual_chunker import ManualChunkData


@override_settings(MODEL_CONFIG_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
class ModelConfigurationApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="model-admin",
            password="test-password",
            is_staff=True,
        )
        self.user = get_user_model().objects.create_user(
            username="ordinary-user",
            password="test-password",
        )

    def test_staff_can_save_encrypted_keys_and_models(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "openai_api_key": "sk-openai-secret-1234",
                "gemini_api_key": "gemini-secret-5678",
                "vision_model": "gemini-2.5-pro",
                "chat_provider": "openai",
                "chat_model": "gpt-5.4-mini",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        openai_stored = ApiKey.objects.get(name=SECRET_OPENAI).value
        gemini_stored = ApiKey.objects.get(name=SECRET_GEMINI).value
        self.assertTrue(openai_stored.startswith("enc:v1:"))
        self.assertTrue(gemini_stored.startswith("enc:v1:"))
        self.assertNotIn("sk-openai-secret", openai_stored)
        self.assertEqual(get_secret(SECRET_OPENAI), "sk-openai-secret-1234")
        self.assertEqual(get_secret(SECRET_GEMINI), "gemini-secret-5678")

        config = ModelConfiguration.objects.get(singleton_id=1)
        self.assertEqual(config.embedding_model, "text-embedding-3-small")
        self.assertEqual(config.vision_model, "gemini-2.5-pro")
        self.assertEqual(config.chat_provider, "openai")
        self.assertEqual(config.chat_model, "gpt-5.4-mini")

        body = response.json()
        serialized = json.dumps(body)
        self.assertNotIn("sk-openai-secret-1234", serialized)
        self.assertNotIn("gemini-secret-5678", serialized)
        self.assertTrue(body["keys"]["openai"]["configured"])

    def test_rejects_model_from_wrong_provider(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "vision_model": "gemini-2.5-flash",
                "chat_provider": "gemini",
                "chat_model": "gpt-5.4-mini",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_non_staff_cannot_read_configuration(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin-panel/api/model-config/")
        self.assertEqual(response.status_code, 403)

    def test_get_never_returns_raw_key(self):
        self.client.force_login(self.staff)
        self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "openai_api_key": "sk-never-return-this-9999",
                "vision_model": "gemini-2.5-flash",
                "chat_provider": "gemini",
                "chat_model": "gemini-2.5-flash",
            }),
            content_type="application/json",
        )
        response = self.client.get("/admin-panel/api/model-config/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sk-never-return-this-9999", response.content.decode())
        self.assertIn("9999", response.json()["keys"]["openai"]["masked"])

    def test_clear_key_suppresses_environment_fallback(self):
        self.client.force_login(self.staff)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env-fallback"}):
            response = self.client.post(
                "/admin-panel/api/model-config/save/",
                data=json.dumps({
                    "clear_openai_key": True,
                    "vision_model": "gemini-2.5-flash",
                    "chat_provider": "gemini",
                    "chat_model": "gemini-2.5-flash",
                }),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(get_secret(SECRET_OPENAI), "")
            self.assertFalse(response.json()["keys"]["openai"]["configured"])


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class CatalogV2ModelTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name='Hand Tools', slug='hand-tools')

    def _document(self, *, checksum: str, version: int, active: bool = False):
        return CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename=f'hand-tools-v{version}.pdf',
            file=SimpleUploadedFile(f'hand-tools-v{version}.pdf', b'%PDF-test'),
            checksum_sha256=checksum,
            version=version,
            is_active=active,
        )

    def test_only_one_document_version_can_be_active(self):
        self._document(checksum='a' * 64, version=1, active=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._document(checksum='b' * 64, version=2, active=True)

    def test_checksum_prevents_duplicate_document_upload(self):
        checksum = hashlib.sha256(b'same-pdf').hexdigest()
        self._document(checksum=checksum, version=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._document(checksum=checksum, version=2)

    def test_family_keeps_raw_and_normalized_categories(self):
        document = self._document(checksum='c' * 64, version=1)
        category = Category.objects.create(name='Hammer', slug='test-hammer')
        family = ProductFamily.objects.create(
            document=document,
            source_key='page-11:chid',
            product_name='Club Hammer',
            product_code='CHID',
            raw_category='Club Hammers Indestructible Handle',
            normalized_category=category,
            page_start=11,
            page_end=11,
        )

        self.assertEqual(family.raw_category, 'Club Hammers Indestructible Handle')
        self.assertEqual(family.normalized_category.slug, 'test-hammer')

    def test_upload_path_uses_stable_ids_and_safe_filename(self):
        document = CatalogDocument(
            catalog=self.catalog,
            original_filename='catalog.pdf',
            checksum_sha256='d' * 64,
            version=1,
        )
        document.file = SimpleUploadedFile('../catalog.pdf', b'%PDF-test')
        document.save()

        self.assertIn(str(self.catalog.id), document.file.name)
        self.assertIn(str(document.id), document.file.name)
        self.assertNotIn('..', document.file.name)

    def test_structured_persistence_is_idempotent_and_makes_no_embeddings(self):
        document = self._document(checksum='e' * 64, version=1)
        products = [{
            'source_key': 'p11:chid',
            'product_name': 'Club Hammer',
            'product_code': 'CHID',
            'raw_category': 'Club Hammers',
            'normalized_category_suggestion': 'club hammer',
            'page_start': 11,
            'page_end': 11,
            'extraction_confidence': 0.95,
            'children': [{
                'product_code': 'CHID',
                'product_name': 'Club Hammer',
                'ordering_table': [
                    {'Cat_No': 'CHID/2.5/12', 'Ord_No': '34500', 'Size': '2.5 lb'},
                    {'Cat_No': 'CHID/4/12', 'Ord_No': '34501', 'Size': '4 lb'},
                ],
            }],
        }]

        first = persist_assembled_products(document, products)
        second = persist_assembled_products(document, products)

        self.assertEqual(first['embedding_calls'], 0)
        self.assertEqual(second['embedding_calls'], 0)
        self.assertEqual(ProductFamily.objects.filter(document=document).count(), 1)
        self.assertEqual(ProductVariant.objects.filter(family__document=document).count(), 2)
        self.assertEqual(DocumentChunk.objects.filter(document=document).count(), 1)
        family = ProductFamily.objects.get(document=document)
        self.assertEqual(family.review_status, ProductFamily.ReviewStatus.APPROVED)
        document.refresh_from_db()
        self.assertEqual(document.status, CatalogDocument.Status.INDEXING)

    def test_uncertain_extraction_is_sent_to_review(self):
        document = self._document(checksum='f' * 64, version=1)
        result = persist_assembled_products(document, [{
            'source_key': 'p20:unknown',
            'product_name': 'Unknown Product 20',
            'raw_category': 'GROZ',
            'page_start': 20,
            'page_end': 20,
            'extraction_confidence': 0.2,
            'children': [],
        }])

        self.assertEqual(result['needs_review'], 1)
        document.refresh_from_db()
        self.assertEqual(document.status, CatalogDocument.Status.REVIEW)

    def test_legacy_markdown_override_is_preserved_byte_for_byte(self):
        document = self._document(checksum='1' * 64, version=1)
        original = '# Legacy Product\n\nExact content.\n\n'
        persist_assembled_products(document, [{
            'source_key': 'legacy:1',
            'product_name': 'Legacy Product',
            'page_start': 1,
            'page_end': 1,
            '_chunk_text': original,
        }])

        self.assertEqual(DocumentChunk.objects.get(document=document).text, original)


@override_settings(CATALOG_RAG_V2_INGEST=True)
class CatalogV2IngestionApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username='catalog-v2-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.staff)

    @staticmethod
    def _pdf_upload(name='catalog.pdf'):
        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(buffer)
        return SimpleUploadedFile(name, buffer.getvalue(), content_type='application/pdf')

    def test_v2_upload_registers_document_and_pending_job(self):
        response = self.client.post('/admin-panel/api/v2/upload/', {
            'pdf': self._pdf_upload(),
            'catalog_name': 'Test Tools',
            'source_type': 'catalog',
        })

        self.assertEqual(response.status_code, 201)
        document = CatalogDocument.objects.get(pk=response.json()['document_id'])
        job = IngestionJob.objects.get(pk=response.json()['job_id'])
        self.assertEqual(document.page_count, 1)
        self.assertEqual(document.status, CatalogDocument.Status.UPLOADED)
        self.assertEqual(job.status, IngestionJob.Status.PENDING)

    def test_duplicate_checksum_is_rejected_before_processing(self):
        first = self.client.post('/admin-panel/api/v2/upload/', {'pdf': self._pdf_upload()})
        second = self.client.post('/admin-panel/api/v2/upload/', {'pdf': self._pdf_upload('renamed.pdf')})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(CatalogDocument.objects.count(), 1)
        self.assertEqual(IngestionJob.objects.count(), 1)

    @override_settings(CATALOG_RAG_V2_INGEST=False)
    def test_v2_upload_is_blocked_while_feature_flag_is_off(self):
        response = self.client.post('/admin-panel/api/v2/upload/', {'pdf': self._pdf_upload()})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(CatalogDocument.objects.count(), 0)


class CatalogV2WorkerTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            PROJECT_ROOT=Path(self.temp_dir.name),
            MEDIA_ROOT=Path(self.temp_dir.name) / 'media',
        )
        self.settings_override.enable()
        self.catalog = Catalog.objects.create(name='Worker Catalog', slug='worker-catalog')
        self.document = CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename='worker.pdf',
            file=SimpleUploadedFile('worker.pdf', b'%PDF-test'),
            checksum_sha256='9' * 64,
            version=1,
            page_count=1,
        )
        self.job = IngestionJob.objects.create(
            document=self.document,
            total_units=1,
        )

    def tearDown(self):
        self.settings_override.disable()
        self.temp_dir.cleanup()

    @patch('catalog.services.jobs.subprocess.run')
    @patch('catalog.services.jobs.subprocess_environment', return_value={})
    def test_worker_persists_assembled_products_without_embedding(self, _, run_mock):
        artifact = Path(self.temp_dir.name) / 'vision_pipeline' / 'data' / str(self.document.id)
        artifact.mkdir(parents=True)
        product = {
            'source_key': 'p1:hammer',
            'product_name': 'Club Hammer',
            'product_code': 'CHID',
            'raw_category': 'Club Hammer',
            'normalized_category_suggestion': 'club hammer',
            'page_num': 1,
            'original_page_num': 1,
            'page_start': 1,
            'page_end': 1,
            'extraction_confidence': 0.95,
            'children': [{'product_code': 'CHID'}],
        }
        (artifact / 'assembled_products.json').write_text(json.dumps([product]))
        (artifact / 'products.json').write_text(json.dumps([product]))
        run_mock.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')

        result = run_ingestion_job(str(self.job.id), allow_disabled=True)

        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['embedding_calls'], 0)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, IngestionJob.Status.SUCCEEDED)
        self.assertEqual(ExtractionPage.objects.get(document=self.document).status, ExtractionPage.Status.EXTRACTED)


class CatalogV2IndexingTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name='Index Catalog', slug='index-catalog')
        self.document = CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename='index.pdf',
            file=SimpleUploadedFile('index.pdf', b'%PDF-test'),
            checksum_sha256='8' * 64,
            version=1,
            page_count=1,
        )
        persist_assembled_products(self.document, [{
            'source_key': 'p1:chid',
            'product_name': 'Club Hammer',
            'product_code': 'CHID',
            'raw_category': 'Club Hammer',
            'normalized_category_suggestion': 'club hammer',
            'page_start': 1,
            'page_end': 1,
            'extraction_confidence': 0.95,
            'children': [{'product_code': 'CHID'}],
        }])

    def test_dry_run_reports_exact_paid_embedding_count(self):
        report = index_dry_run(self.document)

        self.assertEqual(report.total_chunks, 1)
        self.assertEqual(report.pending_embeddings, 1)
        self.assertEqual(report.review_blocked, 0)

    @patch('catalog.services.indexing._activate_document_payload')
    @patch('catalog.services.indexing.reconcile_document')
    @patch('catalog.services.indexing.build_vector_store')
    @patch('catalog.services.indexing.ensure_collection')
    @patch('catalog.services.indexing.get_runtime_config')
    def test_confirmed_index_marks_document_ready(
        self,
        runtime_mock,
        ensure_mock,
        store_mock,
        reconcile_mock,
        activate_mock,
    ):
        runtime_mock.return_value = SimpleNamespace(openai_api_key='sk-test')
        vector_store = Mock()
        store_mock.return_value = vector_store
        reconcile_mock.return_value = {
            'document_id': str(self.document.id),
            'expected': 1,
            'actual': 1,
            'missing': [],
            'unexpected': [],
        }

        result = index_document(
            self.document,
            confirmed_embedding_count=1,
            allow_disabled=True,
        )

        self.assertEqual(result['indexed'], 1)
        vector_store.add_documents.assert_called_once()
        self.document.refresh_from_db()
        self.assertTrue(self.document.is_active)
        self.assertEqual(self.document.status, CatalogDocument.Status.READY)
        self.assertEqual(DocumentChunk.objects.get(document=self.document).index_status, DocumentChunk.IndexStatus.INDEXED)
        ensure_mock.assert_called_once()
        activate_mock.assert_called_once()

    def test_wrong_confirmation_stops_before_embedding(self):
        with self.assertRaisesMessage(ValueError, 'Embedding confirmation mismatch'):
            index_document(
                self.document,
                confirmed_embedding_count=0,
                allow_disabled=True,
            )

    def test_v2_payload_has_root_category_subcategory_and_legacy_schema(self):
        self.document.source_schema = 'legacy-v1'
        self.document.extraction_schema_version = 1
        self.document.save(update_fields=('source_schema', 'extraction_schema_version', 'updated_at'))
        chunk = DocumentChunk.objects.select_related(
            'document', 'family', 'family__normalized_category', 'family__normalized_category__parent'
        ).get(document=self.document)
        chunk.extraction_schema_version = 1
        metadata = _metadata(chunk)

        self.assertEqual(metadata['schema_version'], 2)
        self.assertEqual(metadata['source_schema'], 'legacy-v1')
        self.assertEqual(metadata['normalized_category'], 'hammer')
        self.assertEqual(metadata['normalized_subcategory'], 'club-hammer')
        self.assertEqual(metadata['language'], 'en')


class CatalogV2QueryEngineTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name='Query Catalog', slug='query-catalog')
        self.document = CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename='query.pdf',
            file=SimpleUploadedFile('query.pdf', b'%PDF-test'),
            checksum_sha256='7' * 64,
            version=1,
            page_count=2,
        )
        persist_assembled_products(self.document, [
            {
                'source_key': 'p1:club',
                'product_name': 'Club Hammer',
                'product_code': 'CHID',
                'raw_category': 'Club Hammer',
                'normalized_category_suggestion': 'club hammer',
                'page_start': 1,
                'page_end': 1,
                'extraction_confidence': 0.95,
                'children': [{'product_code': 'CHID/4/12'}],
            },
            {
                'source_key': 'p2:copper',
                'product_name': 'Copper Hammer',
                'product_code': 'CHID-CU',
                'raw_category': 'Copper Hammer',
                'normalized_category_suggestion': 'copper hammer',
                'page_start': 2,
                'page_end': 2,
                'extraction_confidence': 0.95,
                'children': [{'product_code': 'CHID/4/12/CU'}],
            },
        ])
        self.document.status = CatalogDocument.Status.READY
        self.document.is_active = True
        self.document.save(update_fields=('status', 'is_active', 'updated_at'))
        self.runtime = SimpleNamespace(
            chat_provider='openai',
            chat_api_key='sk-test',
            chat_model='gpt-4o-mini',
            openai_api_key='sk-test',
        )

    def test_root_category_lists_all_descendant_hammer_families(self):
        result = list_products(category_value='hammers', scope=QueryScope(), page_size=50)

        self.assertEqual(result.total, 2)
        self.assertTrue(result.complete)
        self.assertEqual({item['product_code'] for item in result.products}, {'CHID', 'CHID-CU'})

    def test_same_code_with_different_product_names_keeps_both_records(self):
        other_catalog = Catalog.objects.create(name='Other Query Catalog', slug='other-query-catalog')
        other_document = CatalogDocument.objects.create(
            catalog=other_catalog,
            original_filename='other-query.pdf',
            file=SimpleUploadedFile('other-query.pdf', b'%PDF-test'),
            checksum_sha256='5' * 64,
            version=1,
            page_count=1,
        )
        persist_assembled_products(other_document, [{
            'source_key': 'p1:club-duplicate',
            'product_name': 'Club Hammer Duplicate',
            'product_code': 'CHID',
            'raw_category': 'Club Hammer',
            'normalized_category_suggestion': 'club hammer',
            'description': 'Short duplicate listing.',
            'page_start': 1,
            'page_end': 1,
            'extraction_confidence': 0.95,
            'children': [{'product_code': 'CHID'}],
        }])
        other_document.status = CatalogDocument.Status.READY
        other_document.is_active = True
        other_document.save(update_fields=('status', 'is_active', 'updated_at'))

        products = find_by_product_codes(['CHID'], scope=QueryScope())

        self.assertEqual(len(products), 2)
        self.assertEqual(
            {product['product_name'] for product in products},
            {'Club Hammer', 'Club Hammer Duplicate'},
        )

    def test_hammer_mechanism_heading_is_not_a_hammer_product(self):
        category = resolve_category(
            product_name='IMPROVED TWIN HAMMER MECHANISM',
            raw_category='HAMMER MECHANISM',
        )

        self.assertIsNone(category)

    def test_code_validation_rejects_hyphenated_properties(self):
        resolved = resolve_product_code_tokens(
            ['NON-SPARKING', 'HEAVY-DUTY', 'CHID'],
            scope=QueryScope(),
        )

        self.assertEqual(resolved, ['CHID'])

    @patch('catalog.services.query_engine.LLMAnswerer.answer')
    @patch(
        'catalog.services.query_engine.LLMAnswerer.exhaustive_introduction',
        return_value='These matching hammers are listed below.',
    )
    def test_exhaustive_query_never_uses_top_k_or_llm_table(self, intro_mock, answer_mock):
        execution = CatalogQueryEngine(self.runtime).execute('What hammers are available?')

        self.assertEqual(execution.plan.intent, QueryIntent.EXHAUSTIVE_LIST)
        self.assertEqual(execution.total_results, 2)
        self.assertTrue(execution.complete_result)
        self.assertEqual(len(execution.chunks), 2)
        self.assertIn('| Club Hammer | CHID |', execution.answer)
        self.assertIn('| Copper Hammer | CHID-CU |', execution.answer)
        self.assertIn('Showing 1-2 of 2 matching products.', execution.answer)
        self.assertIn('Complete result for the selected catalog scope.', execution.answer)
        intro_mock.assert_called_once()
        answer_mock.assert_not_called()

    @patch('catalog.services.query_engine.LLMAnswerer.answer')
    def test_aggregation_count_is_rendered_without_llm(self, answer_mock):
        execution = CatalogQueryEngine(self.runtime).execute(
            'How many hammer product families are available in this catalog?'
        )

        self.assertEqual(execution.total_results, 2)
        self.assertEqual(
            execution.answer,
            'There are 2 active hammer product families in the selected catalog scope.',
        )
        answer_mock.assert_not_called()

    def test_deterministic_exhaustive_renderer_keeps_all_35_rows(self):
        products = [
            {
                'product_name': f'Hammer {number}',
                'product_code': f'H-{number}',
                'category': 'Hammer',
                'source': {
                    'source_pdf': 'hand-tools.pdf',
                    'page_start': number,
                    'page_end': number,
                },
            }
            for number in range(1, 36)
        ]

        answer = _render_exhaustive_answer(
            'Matching hammers:',
            products,
            total_results=35,
            page=1,
            page_size=100,
            complete_result=True,
        )

        for number in range(1, 36):
            self.assertIn(f'| {number} | Hammer {number} | H-{number} |', answer)
        rendered_rows = [
            line for line in answer.splitlines()
            if line.startswith('| ') and '| Hammer ' in line and '| H-' in line
        ]
        self.assertEqual(len(rendered_rows), 35)
        self.assertIn('Showing 1-35 of 35 matching products.', answer)

    def test_answer_constraint_check_normalizes_hyphens_and_units(self):
        answer = 'Non sparking options include a lightest 2.5 lb model and 4 lbs model.'

        missing = _missing_answer_constraints(
            answer,
            ['non-sparking', 'lightest', '4 lb', 'heaviest'],
        )

        self.assertEqual(missing, ['heaviest'])

    def test_application_status_removes_model_completeness_contradiction(self):
        answer = _append_planned_result_status(
            'The requested product tables follow.\nThe shortlist is not exhaustive.\nRecommendations are included.',
            complete_result=True,
            missing_tasks=[],
            has_exhaustive_inventory=True,
        )

        self.assertNotIn('not exhaustive', answer.lower())
        self.assertIn('Recommendations are included.', answer)
        self.assertIn(
            'Complete for the selected catalog scope and planned inventory filters.',
            answer,
        )

    def test_numeric_variant_constraints_are_selected_deterministically(self):
        chunks = [{
            'text': '''| CAT NR. | ORD NR. | HEAD WEIGHT (LBS) | OVERALL LENGTH (INCH) |
| --- | --- | --- | --- |
| CHID/2.5/12/CU | 34602 | 2.5 | 12 |
| CHID/4/12/CU | 34600 | 4 | 12 |
| SHID/14/30/CU | 34611 | 14 | 30 |''',
            'metadata': {'source_pdf': 'hand-tools.pdf', 'page_start': 15},
            'score': 1.0,
        }]

        table = _render_constraint_matches(
            chunks,
            ['lightest', '4 lb', 'heaviest'],
        )

        self.assertIn('| Lightest | CHID/2.5/12/CU | 34602 | 2.5 | 12 |', table)
        self.assertIn('| 4 lb | CHID/4/12/CU | 34600 | 4 | 12 |', table)
        self.assertIn('| Heaviest | SHID/14/30/CU | 34611 | 14 | 30 |', table)

    def test_weight_and_length_constraints_are_applied_as_pairs(self):
        chunks = [{
            'text': '''| CAT NR. | ORD NR. | HEAD WEIGHT (LBS) | OVERALL LENGTH (INCH) |
| --- | --- | --- | --- |
| SHID/6/30/CU | 34616 | 6 | 30 |
| SHID/6/16/BR | 34707 | 6 | 16 |
| SHID/8/24/CU | 34610 | 8 | 24 |
| SHID/8/30/BR | 34706 | 8 | 30 |''',
            'metadata': {'source_pdf': 'hand-tools.pdf', 'page_start': 15},
            'score': 1.0,
        }]

        table = _render_constraint_matches(
            chunks,
            ['shortest 6 lb', 'longest 8 lb'],
        )

        self.assertIn('| shortest 6 lb | SHID/6/16/BR | 34707 | 6 | 16 |', table)
        self.assertIn('| longest 8 lb | SHID/8/30/BR | 34706 | 8 | 30 |', table)

    def test_light_length_and_heavy_long_handle_are_selected_deterministically(self):
        chunks = [{
            'text': '''| CAT NR. | ORD NR. | HEAD WEIGHT (LBS) | OVERALL LENGTH (INCH) |
| --- | --- | --- | --- |
| CHID/2.5/12/CU | 34602 | 2.5 | 12 |
| CHID/4/12/CU | 34600 | 4 | 12 |
| SHID/8/24/CU | 34610 | 8 | 24 |
| SHID/8/30/BR | 34706 | 8 | 30 |''',
            'metadata': {
                'product_name': 'Copper and Brass Hammers',
                'source_pdf': 'hand-tools.pdf',
                'page_start': 15,
            },
            'score': 1.0,
        }]

        table = _render_constraint_matches(
            chunks,
            ['light 12 inch', 'heavy long-handle'],
            'Compare copper and brass options.',
        )

        self.assertIn('| light 12 inch | CHID/2.5/12/CU | 34602 | 2.5 | 12 |', table)
        self.assertIn('| heavy long-handle | SHID/8/30/BR | 34706 | 8 | 30 |', table)

    def test_verified_variant_table_removes_unsupported_model_code(self):
        chunks = [{
            'text': '''| CAT NR. | ORD NR. | HEAD WEIGHT (LBS) | OVERALL LENGTH (INCH) |
| --- | --- | --- | --- |
| SHID/8/24/CU | 34610 | 8 | 24 |
| SHID/14/30/CU | 34611 | 14 | 30 |''',
            'metadata': {
                'product_name': 'Copper Head Sledge Hammers',
                'source_pdf': 'hand-tools.pdf',
                'page_start': 15,
            },
            'score': 1.0,
        }]

        cleaned = _remove_unverified_variant_code_lines(
            'Valid: SHID/8/24/CU\nUnsupported: SHID/8/30/CU',
            chunks,
        )
        table = _render_verified_variant_table(chunks)

        self.assertIn('SHID/8/24/CU', cleaned)
        self.assertNotIn('SHID/8/30/CU', cleaned)
        self.assertIn('| Copper Head Sledge Hammers | SHID/14/30/CU | 34611 |', table)

    def test_requested_weight_and_materials_narrow_verified_rows_and_matches(self):
        chunks = [{
            'text': '''| CAT NR. | ORD NR. | HEAD WEIGHT (LBS) | OVERALL LENGTH (INCH) |
| --- | --- | --- | --- |
| CHID/2.5/12/CU | 34602 | 2.5 | 12 |
| CHID/4/12/CU | 34600 | 4 | 12 |
| SHID/4/12/BR | 34702 | 4 | 12 |
| SHID/6/30/BR | 62263 | 6 | 30 |''',
            'metadata': {
                'product_name': 'Copper and Brass Hammers',
                'source_pdf': 'hand-tools.pdf',
                'page_start': 15,
            },
            'score': 1.0,
        }]
        query = 'Identify the 4 lb copper and brass choices with catalog and order numbers.'

        variants = _render_verified_variant_table(chunks, query)
        matches = _render_constraint_matches(chunks, ['4 lb'], query)

        self.assertNotIn('CHID/2.5/12/CU', variants)
        self.assertNotIn('SHID/6/30/BR', variants)
        self.assertIn('CHID/4/12/CU', variants)
        self.assertIn('SHID/4/12/BR', variants)
        self.assertIn('| 4 lb copper | CHID/4/12/CU | 34600 |', matches)
        self.assertIn('| 4 lb brass | SHID/4/12/BR | 34702 |', matches)

    def test_model_selected_options_are_removed_before_deterministic_matches(self):
        answer = '''Copper and brass families were compared.

### Selected Options
**Light option**
| Product Code: | Order Number: |
|---|---|
| CHID/4/12/CU | 34600 |

This model selection may be wrong.'''

        cleaned = _strip_model_constraint_selections(answer)

        self.assertEqual(cleaned, 'Copper and brass families were compared.')

    def test_empty_model_sections_are_removed_before_application_tables(self):
        answer = '''### Complete Inventory

#### Brass Options

#### Copper Options

### Verified variant and order data

| CAT NR. | ORD NR. |
|---|---|'''

        cleaned = _strip_empty_markdown_headings(answer)

        self.assertNotIn('Complete Inventory', cleaned)
        self.assertNotIn('Brass Options', cleaned)
        self.assertNotIn('Copper Options', cleaned)
        self.assertIn('Verified variant and order data', cleaned)

    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Detailed CHID answer')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_exact_detail_query_adds_semantic_catalog_evidence(self, retrieve_mock, _):
        retrieve_mock.return_value = [{
            'text': 'Original CHID Markdown specifications',
            'metadata': {
                'chunk_hash': 'chid-markdown',
                'product_name': 'Club Hammer',
                'product_code': 'CHID',
            },
            'score': 0.9,
        }]

        execution = CatalogQueryEngine(self.runtime).execute('Show detailed variants for CHID.')

        retrieve_mock.assert_called_once_with(
            'Show detailed variants for CHID.',
            QueryScope(),
            top_k=5,
        )
        self.assertGreaterEqual(len(execution.chunks), 2)

    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='CHID details')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve', return_value=[])
    def test_short_family_code_is_resolved_by_postgres(self, _, __):
        execution = CatalogQueryEngine(self.runtime).execute('Show CHID details')

        self.assertEqual(execution.plan.intent, QueryIntent.EXACT_LOOKUP)
        self.assertEqual(execution.total_results, 1)
        self.assertEqual(execution.sources[0]['code'], 'CHID')

    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Grounded kit')
    @patch('catalog.services.query_engine.enrich_complex_plan')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_multi_intent_runs_one_retrieval_per_subquery(self, retrieve_mock, enrich_mock, _):
        from rag_pipeline.planner import QueryPlan

        enrich_mock.return_value = QueryPlan(
            intent=QueryIntent.MULTI_INTENT,
            scope=QueryScope(),
            subqueries=['find a hammer', 'find a wrench'],
        )
        retrieve_mock.side_effect = [
            [{'text': 'hammer', 'metadata': {'chunk_hash': 'h1', 'product_name': 'Hammer'}, 'score': 0.8}],
            [{'text': 'wrench', 'metadata': {'chunk_hash': 'w1', 'product_name': 'Wrench'}, 'score': 0.7}],
        ]

        execution = CatalogQueryEngine(self.runtime).execute('Build a kit with a hammer and wrench')

        self.assertEqual(retrieve_mock.call_count, 2)
        self.assertEqual(len(execution.chunks), 2)
        self.assertTrue(execution.complete_result)

    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Grounded complex answer')
    @patch('catalog.services.query_engine.enrich_complex_plan')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_complex_plan_executes_postgres_inventory_and_hybrid_tasks(
        self,
        retrieve_mock,
        enrich_mock,
        _,
    ):
        enrich_mock.return_value = QueryPlan(
            intent=QueryIntent.MULTI_INTENT,
            intents=[
                QueryIntent.EXHAUSTIVE_LIST,
                QueryIntent.COMPARISON,
                QueryIntent.RECOMMENDATION,
            ],
            scope=QueryScope(),
            tasks=[
                QueryTask(
                    route=QueryRoute.POSTGRES_INVENTORY,
                    purpose='Copper hammer inventory',
                    query='all copper hammers',
                    categories=['hammer'],
                    materials=['copper'],
                    exhaustive=True,
                ),
                QueryTask(
                    route=QueryRoute.HYBRID_SEARCH,
                    purpose='Copper hammer specifications',
                    query='copper hammer variants and specifications',
                ),
            ],
            used_model_planner=True,
        )
        retrieve_mock.return_value = [{
            'text': 'Copper hammer variant details',
            'metadata': {
                'chunk_hash': 'semantic-copper',
                'product_name': 'Copper Hammer Details',
                'product_code': 'CHID-CU',
            },
            'score': 0.8,
        }]

        execution = CatalogQueryEngine(self.runtime).execute(
            'List every copper hammer, compare suitability, and recommend the best option.'
        )

        retrieve_mock.assert_called_once_with(
            'copper hammer variants and specifications',
            QueryScope(),
            top_k=5,
        )
        self.assertTrue(execution.complete_result)
        self.assertEqual(execution.plan.intent, QueryIntent.MULTI_INTENT)
        self.assertIn('CHID-CU', {source['code'] for source in execution.sources})
        self.assertIn(
            'Complete for the selected catalog scope and planned inventory filters.',
            execution.answer,
        )

    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Copper products found')
    @patch('catalog.services.query_engine.enrich_complex_plan')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_invalid_exact_planner_task_is_normalized_to_hybrid(
        self,
        retrieve_mock,
        enrich_mock,
        _,
    ):
        enrich_mock.return_value = QueryPlan(
            intent=QueryIntent.MULTI_INTENT,
            intents=[QueryIntent.EXACT_LOOKUP, QueryIntent.RECOMMENDATION],
            scope=QueryScope(),
            tasks=[QueryTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find a 4 lb copper hammer',
                query='4 lb copper hammer with shortest handle',
                product_codes=[],
            )],
            used_model_planner=True,
        )
        retrieve_mock.return_value = [{
            'text': 'Copper hammer details',
            'metadata': {
                'chunk_hash': 'copper-details',
                'product_name': 'Copper Hammer',
                'product_code': 'CHID-CU',
            },
            'score': 0.9,
        }]

        execution = CatalogQueryEngine(self.runtime).execute(
            'Find the following item: a 4 lb copper hammer with the shortest handle.'
        )

        self.assertEqual(execution.plan.tasks[0].route, QueryRoute.HYBRID_SEARCH)
        retrieve_mock.assert_called_once_with(
            '4 lb copper hammer with shortest handle',
            QueryScope(),
            top_k=5,
        )
        self.assertEqual(execution.sources[0]['code'], 'CHID-CU')

    def test_inventory_task_infers_valid_category_and_material_filters(self):
        plan = QueryPlan(
            intent=QueryIntent.MULTI_INTENT,
            scope=QueryScope(),
            tasks=[QueryTask(
                route=QueryRoute.POSTGRES_INVENTORY,
                purpose='List all copper and brass hammer families',
                query='complete copper and brass hammer inventory',
                categories=[],
                materials=[],
                exhaustive=True,
            )],
            used_model_planner=True,
        )

        normalized = CatalogQueryEngine(self.runtime)._normalize_planned_tasks(plan)

        self.assertEqual(normalized.tasks[0].categories, ['hammer'])
        self.assertEqual(normalized.tasks[0].materials, ['copper', 'brass'])


class CatalogV2ManualIngestionTests(TestCase):
    def test_manual_chunks_are_source_typed_and_product_linked_without_embeddings(self):
        catalog = Catalog.objects.create(name='Manual Catalog', slug='manual-catalog')
        document = CatalogDocument.objects.create(
            catalog=catalog,
            source_type=CatalogDocument.SourceType.MANUAL,
            original_filename='ipw-manual.pdf',
            file=SimpleUploadedFile('ipw-manual.pdf', b'%PDF-test'),
            checksum_sha256='6' * 64,
            version=1,
            page_count=1,
        )
        result = persist_manual_chunks(document, [
            ManualChunkData(
                title='IPW-304 Maintenance',
                text='Disconnect the air supply before service.',
                page_start=1,
                page_end=1,
                ordinal=0,
                linked_product_codes=['IPW-304'],
            )
        ])

        chunk = DocumentChunk.objects.get(document=document)
        self.assertEqual(result['embedding_calls'], 0)
        self.assertEqual(chunk.chunk_type, DocumentChunk.ChunkType.MANUAL_SECTION)
        self.assertEqual(chunk.linked_product_codes, ['IPW-304'])
        document.refresh_from_db()
        self.assertEqual(document.status, CatalogDocument.Status.INDEXING)
