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
from .services.chat_memory import ADMIN_CHAT_MEMORY_KEY, PUBLIC_CHAT_MEMORY_KEY, build_memory_update
from .services.query_repository import list_products
from .services.query_repository import find_by_product_codes
from .services.query_repository import resolve_product_code_tokens
from .services.manual_ingestion import persist_manual_chunks
from .services.taxonomy import resolve_category
from .views import _clean_pipeline_error
from rag_pipeline.ai_router import AIRouterPlan, AIRouterResult, AIRouterTask
from rag_pipeline.ai_router import AIQueryRouterError
from rag_pipeline.planner import (
    QueryEntity,
    QueryIntent,
    QueryPlan,
    QueryRoute,
    QueryScope,
    QueryTask,
    deterministic_plan,
    should_use_model_planner,
)
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

    def _force_admin_panel_login(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session['admin_access_token'] = 'test-admin-token'
        session.save()

    def test_staff_can_save_encrypted_keys_and_models(self):
        self._force_admin_panel_login()
        response = self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "openai_api_key": "sk-openai-secret-1234",
                "gemini_api_key": "gemini-secret-5678",
                "vision_model": "gemini-2.5-pro",
                "chat_provider": "openai",
                "chat_model": "gpt-4o-mini",
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
        self.assertEqual(config.chat_model, "gpt-4o-mini")

        body = response.json()
        serialized = json.dumps(body)
        self.assertNotIn("sk-openai-secret-1234", serialized)
        self.assertNotIn("gemini-secret-5678", serialized)
        self.assertTrue(body["keys"]["openai"]["configured"])

    def test_rejects_model_from_wrong_provider(self):
        self._force_admin_panel_login()
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
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin-panel/login/', response['Location'])

    def test_get_never_returns_raw_key(self):
        self._force_admin_panel_login()
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
        self._force_admin_panel_login()
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


class PipelineErrorMessageTests(TestCase):
    def test_clean_pipeline_error_skips_traceback_caret_noise(self):
        output = '''
Traceback (most recent call last):
  File "/tmp/example.py", line 10, in <module>
    response = model.invoke([
                            ^
google.api_core.exceptions.InvalidArgument: 400 Request contains an invalid argument.
                            ^
'''

        self.assertEqual(
            _clean_pipeline_error(output),
            '400 Request contains an invalid argument.',
        )

    def test_clean_pipeline_error_skips_tqdm_progress_noise(self):
        output = '''
WARNING: 1 pages failed: [10]
Pages: 100%|██████████| 1/1 [01:07<00:00, 67.77s/page]
============================================================
Vision Pipeline Complete!
  Pages processed : 0
  Products found  : 0
  Families        : 0
============================================================
'''

        self.assertEqual(_clean_pipeline_error(output), '1 pages failed: [10]')

    def test_clean_pipeline_error_maps_gemini_high_demand(self):
        output = 'google.api_core.exceptions.ServiceUnavailable: 503 UNAVAILABLE. Gemini is experiencing high demand.'

        self.assertEqual(
            _clean_pipeline_error(output),
            'Google Gemini is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.',
        )


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class CatalogDeletePdfApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username='delete-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.staff)
        session = self.client.session
        session['admin_access_token'] = 'test-admin-token'
        session.save()
        self.catalog = Catalog.objects.create(name='Delete Catalog', slug='delete-catalog')

    def _ready_document(self, original_filename: str) -> CatalogDocument:
        document = CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename=original_filename,
            file=SimpleUploadedFile(f'{original_filename}.pdf', b'%PDF-test'),
            checksum_sha256=hashlib.sha256(original_filename.encode()).hexdigest(),
            version=1,
            page_count=2,
            status=CatalogDocument.Status.READY,
            is_active=True,
        )
        DocumentChunk.objects.create(
            document=document,
            chunk_type=DocumentChunk.ChunkType.PRODUCT_FAMILY,
            text='Cordless drill chunk',
            page_start=2,
            page_end=3,
            content_hash=hashlib.sha256(f'{original_filename}:chunk'.encode()).hexdigest(),
            ordinal=1,
            index_status=DocumentChunk.IndexStatus.INDEXED,
        )
        return document

    @patch('catalog.services.indexing.deactivate_document_points')
    @patch('rag_pipeline.providers.build_qdrant_client')
    def test_delete_pdf_matches_stem_stored_v2_document(self, qdrant_mock, deactivate_mock):
        qdrant_mock.return_value.collection_exists.return_value = False
        document = self._ready_document('Cordless_Drill_custom_p0002-0003')

        stats_before = self.client.get('/admin-panel/api/stats/').json()
        self.assertIn(
            'Cordless_Drill_custom_p0002-0003.pdf',
            {item['name'] for item in stats_before['processed']},
        )

        response = self.client.post(
            '/admin-panel/api/delete-pdf/',
            data=json.dumps({'filename': 'Cordless_Drill_custom_p0002-0003.pdf'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted_documents'], 1)
        self.assertFalse(CatalogDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(DocumentChunk.objects.filter(document_id=document.pk).exists())
        deactivate_mock.assert_called_once()
        stats_after = self.client.get('/admin-panel/api/stats/').json()
        self.assertNotIn(
            'Cordless_Drill_custom_p0002-0003.pdf',
            {item['name'] for item in stats_after['processed']},
        )


@override_settings(CATALOG_RAG_V2_INGEST=True)
class CatalogV2IngestionApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username='catalog-v2-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.staff)
        session = self.client.session
        session['admin_access_token'] = 'test-admin-token'
        session.save()

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

    def _router_result(self, plan: AIRouterPlan) -> AIRouterResult:
        return AIRouterResult(
            plan=plan,
            provider=self.runtime.chat_provider,
            model=self.runtime.chat_model,
        )

    def test_root_category_lists_all_descendant_hammer_families(self):
        result = list_products(category_value='hammers', scope=QueryScope(), page_size=50)

        self.assertEqual(result.total, 2)
        self.assertTrue(result.complete)
        self.assertEqual({item['product_code'] for item in result.products}, {'CHID', 'CHID-CU'})

    def test_active_needs_review_families_are_queryable_until_rejected(self):
        family = ProductFamily.objects.create(
            document=self.document,
            source_key='p3:drill',
            product_name='Cordless Impact Drill Driver',
            product_code='BLMD-358JST',
            raw_category='Cordless Impact Drill Driver',
            review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW,
            page_start=3,
            page_end=3,
        )

        result = list_products(scope=QueryScope(document_ids=[str(self.document.id)]), page_size=50)
        self.assertIn('BLMD-358JST', {item['product_code'] for item in result.products})

        family.review_status = ProductFamily.ReviewStatus.REJECTED
        family.save(update_fields=('review_status', 'updated_at'))

        result = list_products(scope=QueryScope(document_ids=[str(self.document.id)]), page_size=50)
        self.assertNotIn('BLMD-358JST', {item['product_code'] for item in result.products})

    def test_all_about_query_uses_semantic_rag_plan(self):
        plan = deterministic_plan(
            'Summaries all about Cordless Impact Drill Driver.',
            QueryScope(document_ids=[str(self.document.id)]),
        )

        self.assertEqual(plan.intent, QueryIntent.GENERAL_SEMANTIC)

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

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer')
    @patch(
        'catalog.services.query_engine.LLMAnswerer.exhaustive_introduction',
        return_value='These matching hammers are listed below.',
    )
    def test_exhaustive_query_never_uses_top_k_or_llm_table(self, intro_mock, answer_mock, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show all hammers available in this catalog.',
            intent=QueryIntent.EXHAUSTIVE_LIST,
            intents=[QueryIntent.EXHAUSTIVE_LIST],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            constraints={},
            subqueries=['Show all hammers available in this catalog.'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_INVENTORY,
                purpose='List all hammer families',
                query='Show all hammers available in this catalog.',
                categories=['hammer'],
                exhaustive=True,
            )],
            confidence=0.97,
            memory_used=False,
        ))

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

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer')
    @patch(
        'catalog.services.query_engine.LLMAnswerer.exhaustive_introduction',
        return_value='These matching hammers are listed below.',
    )
    def test_exhaustive_inventory_task_preserves_pagination(self, intro_mock, answer_mock, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show all hammers available in this catalog.',
            intent=QueryIntent.EXHAUSTIVE_LIST,
            intents=[QueryIntent.EXHAUSTIVE_LIST],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            constraints={},
            subqueries=['Show all hammers available in this catalog.'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_INVENTORY,
                purpose='List all hammer families',
                query='Show all hammers available in this catalog.',
                categories=['hammer'],
                exhaustive=True,
            )],
            confidence=0.97,
            memory_used=False,
        ))

        execution = CatalogQueryEngine(self.runtime).execute(
            'What hammers are available?',
            page=2,
            page_size=1,
        )

        self.assertEqual(execution.total_results, 2)
        self.assertTrue(execution.complete_result)
        self.assertEqual(len(execution.chunks), 1)
        self.assertIn('| Copper Hammer | CHID-CU |', execution.answer)
        self.assertIn('Showing 2-2 of 2 matching products.', execution.answer)
        self.assertIn('Complete result for the selected catalog scope.', execution.answer)
        intro_mock.assert_called_once()
        answer_mock.assert_not_called()

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer')
    def test_aggregation_count_is_rendered_without_llm(self, answer_mock, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='How many hammer product families are available in this catalog?',
            intent=QueryIntent.AGGREGATION,
            intents=[QueryIntent.AGGREGATION],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            constraints={},
            subqueries=['How many hammer product families are available in this catalog?'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_INVENTORY,
                purpose='Count hammer families',
                query='How many hammer product families are available in this catalog?',
                categories=['hammer'],
                exhaustive=False,
            )],
            confidence=0.95,
            memory_used=False,
        ))

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

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Detailed CHID answer')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_exact_detail_query_adds_semantic_catalog_evidence(self, retrieve_mock, _, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show detailed variants for CHID.',
            intent=QueryIntent.EXACT_LOOKUP,
            intents=[QueryIntent.EXACT_LOOKUP],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='product_code', value='CHID')],
            constraints={},
            subqueries=['Show detailed variants for CHID.'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find verified product code CHID',
                query='Show detailed variants for CHID.',
                product_codes=['CHID'],
            )],
            confidence=0.98,
            memory_used=True,
        ))

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
            QueryScope(source_types=['catalog']),
            top_k=5,
        )
        self.assertGreaterEqual(len(execution.chunks), 2)
        self.assertEqual(execution.standalone_query, 'Show detailed variants for CHID.')

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='CHID details')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve', return_value=[])
    def test_short_family_code_is_resolved_by_postgres(self, _, __, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show CHID details',
            intent=QueryIntent.EXACT_LOOKUP,
            intents=[QueryIntent.EXACT_LOOKUP],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='product_code', value='CHID')],
            constraints={},
            subqueries=['Show CHID details'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find verified product code CHID',
                query='Show CHID details',
                product_codes=['CHID'],
            )],
            confidence=0.99,
            memory_used=True,
        ))

        execution = CatalogQueryEngine(self.runtime).execute('Show CHID details')

        self.assertEqual(execution.plan.intent, QueryIntent.EXACT_LOOKUP)
        self.assertEqual(execution.total_results, 1)
        self.assertEqual(execution.sources[0]['code'], 'CHID')

    @patch('rag_pipeline.planner.deterministic_plan', side_effect=AssertionError('deterministic fallback was called'))
    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='CHID-1 catalog details')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve', return_value=[])
    def test_phase6_exact_chid1_uses_ai_catalog_exact_route(
        self,
        retrieve_mock,
        _,
        router_cls,
        deterministic_mock,
    ):
        ProductFamily.objects.create(
            document=self.document,
            source_key='p3:phase6-chid1',
            product_name='Club Hammer Small',
            product_code='CHID-1',
            raw_category='Club Hammer',
            description='Small club hammer listing.',
            page_start=1,
            page_end=1,
            extraction_confidence=0.95,
        )
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show CHID-1 details',
            intent=QueryIntent.EXACT_LOOKUP,
            intents=[QueryIntent.EXACT_LOOKUP],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='product_code', value='CHID-1')],
            constraints={},
            subqueries=['Show CHID-1 details'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find verified product code CHID-1',
                query='Show CHID-1 details',
                product_codes=['CHID-1'],
            )],
            confidence=0.97,
            memory_used=False,
        ))

        execution = CatalogQueryEngine(self.runtime).execute('show CHID-1 details')

        router_cls.return_value.plan.assert_called_once()
        deterministic_mock.assert_not_called()
        retrieve_mock.assert_called_once_with(
            'Show CHID-1 details',
            QueryScope(source_types=['catalog']),
            top_k=5,
        )
        self.assertEqual(execution.plan.intent, QueryIntent.EXACT_LOOKUP)
        self.assertEqual(execution.plan.tasks[0].route, QueryRoute.POSTGRES_EXACT)
        self.assertEqual(execution.plan.scope.source_types, ['catalog'])
        self.assertEqual(execution.standalone_query, 'Show CHID-1 details')
        self.assertTrue(execution.complete_result)
        self.assertEqual(execution.total_results, 1)
        self.assertEqual(execution.sources[0]['code'], 'CHID-1')

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Grounded kit')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_multi_intent_runs_one_retrieval_per_subquery(self, retrieve_mock, _, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Build a kit with a hammer and wrench',
            intent=QueryIntent.MULTI_INTENT,
            intents=[QueryIntent.MULTI_INTENT],
            scope=QueryScope(source_types=['catalog']),
            constraints={},
            subqueries=['find a hammer', 'find a wrench'],
            tasks=[
                AIRouterTask(
                    route=QueryRoute.HYBRID_SEARCH,
                    purpose='Find a hammer',
                    query='find a hammer',
                ),
                AIRouterTask(
                    route=QueryRoute.HYBRID_SEARCH,
                    purpose='Find a wrench',
                    query='find a wrench',
                ),
            ],
            confidence=0.9,
            memory_used=False,
        ))
        retrieve_mock.side_effect = [
            [{'text': 'hammer', 'metadata': {'chunk_hash': 'h1', 'product_name': 'Hammer'}, 'score': 0.8}],
            [{'text': 'wrench', 'metadata': {'chunk_hash': 'w1', 'product_name': 'Wrench'}, 'score': 0.7}],
        ]

        execution = CatalogQueryEngine(self.runtime).execute('Build a kit with a hammer and wrench')

        self.assertEqual(retrieve_mock.call_count, 2)
        self.assertEqual(len(execution.chunks), 2)
        self.assertTrue(execution.complete_result)

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Use a club hammer for masonry.')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_phase6_memory_followup_recommends_about_previous_hammers(
        self,
        retrieve_mock,
        _,
        router_cls,
    ):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Recommend the best hammer for masonry.',
            intent=QueryIntent.RECOMMENDATION,
            intents=[QueryIntent.RECOMMENDATION],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            constraints={'application': 'masonry'},
            subqueries=['Recommend the best hammer for masonry.'],
            tasks=[AIRouterTask(
                route=QueryRoute.HYBRID_SEARCH,
                purpose='Find hammer recommendations for masonry',
                query='Recommend the best hammer for masonry.',
                categories=['hammer'],
            )],
            confidence=0.9,
            memory_used=True,
        ))
        retrieve_mock.return_value = [{
            'text': 'Club hammers are suitable for masonry work.',
            'metadata': {
                'chunk_hash': 'masonry-hammer',
                'product_name': 'Club Hammer',
                'product_code': 'CHID',
            },
            'score': 0.86,
        }]
        memory = {
            'last_user_query': 'list all hammers',
            'last_standalone_query': 'List all hammers',
            'last_intent': 'exhaustive_list',
            'last_intents': ['exhaustive_list'],
            'last_entities': [{'type': 'category', 'value': 'hammer'}],
            'last_scope': {'source_types': ['catalog']},
            'last_answer_summary': 'Listed all hammer product families.',
            'last_sources': [{'code': 'CHID', 'name': 'Club Hammer'}],
        }

        execution = CatalogQueryEngine(self.runtime).execute(
            'which one is best for masonry?',
            memory=memory,
        )

        _, kwargs = router_cls.return_value.plan.call_args
        self.assertEqual(kwargs['memory']['last_user_query'], 'list all hammers')
        retrieve_mock.assert_called_once_with(
            'Recommend the best hammer for masonry.',
            QueryScope(source_types=['catalog']),
            top_k=5,
        )
        self.assertEqual(execution.plan.intent, QueryIntent.RECOMMENDATION)
        self.assertIn('hammer', [entity.value for entity in execution.plan.entities])
        self.assertIn('masonry', execution.standalone_query.lower())
        self.assertTrue(execution.complete_result)

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Pliers catalog answer.')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_phase6_current_query_overrides_previous_memory(
        self,
        retrieve_mock,
        _,
        router_cls,
    ):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show pliers',
            intent=QueryIntent.GENERAL_SEMANTIC,
            intents=[QueryIntent.GENERAL_SEMANTIC],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='pliers')],
            constraints={},
            subqueries=['Show pliers'],
            tasks=[AIRouterTask(
                route=QueryRoute.HYBRID_SEARCH,
                purpose='Find pliers catalog results',
                query='Show pliers',
                categories=['pliers'],
            )],
            confidence=0.88,
            memory_used=False,
        ))
        retrieve_mock.return_value = [{
            'text': 'Pliers catalog result.',
            'metadata': {
                'chunk_hash': 'pliers-1',
                'product_name': 'Combination Pliers',
                'product_code': 'PLR-1',
            },
            'score': 0.82,
        }]
        memory = {
            'last_user_query': 'show CHID-1 details',
            'last_standalone_query': 'Show CHID-1 details',
            'last_intent': 'exact_lookup',
            'last_intents': ['exact_lookup'],
            'last_entities': [{'type': 'product_code', 'value': 'CHID-1'}],
            'last_scope': {'source_types': ['catalog']},
            'last_sources': [{'code': 'CHID-1', 'name': 'Club Hammer Small'}],
        }

        execution = CatalogQueryEngine(self.runtime).execute('show pliers', memory=memory)

        _, kwargs = router_cls.return_value.plan.call_args
        self.assertEqual(kwargs['memory']['last_entities'][0]['value'], 'CHID-1')
        retrieve_mock.assert_called_once_with('Show pliers', QueryScope(source_types=['catalog']), top_k=5)
        self.assertEqual(execution.standalone_query, 'Show pliers')
        self.assertEqual(execution.plan.intent, QueryIntent.GENERAL_SEMANTIC)
        self.assertEqual(execution.plan.entities[0].value, 'pliers')
        self.assertNotIn('CHID-1', execution.standalone_query)
        self.assertTrue(execution.complete_result)

    @patch('catalog.services.query_engine.AIQueryRouter')
    def test_phase6_ambiguous_hammer_requests_clarification_without_memory(self, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='hammer',
            intent=QueryIntent.AMBIGUOUS,
            intents=[QueryIntent.AMBIGUOUS],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            constraints={},
            subqueries=[],
            tasks=[],
            needs_clarification=True,
            clarification_question='Which hammer detail do you want to find?',
            confidence=0.52,
            memory_used=False,
        ))

        execution = CatalogQueryEngine(self.runtime).execute('hammer')

        router_cls.return_value.plan.assert_called_once()
        self.assertTrue(execution.plan.needs_clarification)
        self.assertEqual(execution.answer, 'Which hammer detail do you want to find?')
        self.assertFalse(execution.complete_result)
        self.assertEqual(execution.sources, [])

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Manual repair guidance')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_troubleshooting_hybrid_task_uses_manual_scope(self, retrieve_mock, _, router_cls):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Troubleshoot drill installation failure.',
            intent=QueryIntent.TROUBLESHOOTING,
            intents=[QueryIntent.TROUBLESHOOTING],
            scope=QueryScope(source_types=['manual']),
            constraints={},
            subqueries=['Troubleshoot drill installation failure.'],
            tasks=[AIRouterTask(
                route=QueryRoute.HYBRID_SEARCH,
                purpose='Find manual troubleshooting instructions',
                query='Troubleshoot drill installation failure.',
            )],
            confidence=0.91,
            memory_used=False,
        ))
        retrieve_mock.return_value = [{
            'text': 'Manual says inspect installation and replace worn parts.',
            'metadata': {
                'chunk_hash': 'manual-1',
                'product_name': 'Drill Manual',
                'product_code': '',
                'source_pdf': 'drill-manual.pdf',
                'source_type': 'manual',
            },
            'score': 0.8,
        }]

        execution = CatalogQueryEngine(self.runtime).execute('Why does this drill fail to install?')

        retrieve_mock.assert_called_once_with(
            'Troubleshoot drill installation failure.',
            QueryScope(source_types=['manual']),
            top_k=5,
        )
        self.assertEqual(execution.plan.scope.source_types, ['manual'])
        self.assertIn('Manual repair guidance', execution.answer)
        self.assertIn('All planned retrieval tasks returned supporting evidence.', execution.answer)

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='Grounded complex answer')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_complex_plan_executes_postgres_inventory_and_hybrid_tasks(
        self,
        retrieve_mock,
        _,
        router_cls,
    ):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='List every copper hammer, compare suitability, and recommend the best option.',
            intent=QueryIntent.MULTI_INTENT,
            intents=[
                QueryIntent.EXHAUSTIVE_LIST,
                QueryIntent.COMPARISON,
                QueryIntent.RECOMMENDATION,
            ],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='category', value='hammer')],
            tasks=[
                AIRouterTask(
                    route=QueryRoute.POSTGRES_INVENTORY,
                    purpose='Copper hammer inventory',
                    query='all copper hammers',
                    categories=['hammer'],
                    materials=['copper'],
                    exhaustive=True,
                ),
                AIRouterTask(
                    route=QueryRoute.HYBRID_SEARCH,
                    purpose='Copper hammer specifications',
                    query='copper hammer variants and specifications',
                ),
            ],
            confidence=0.93,
            memory_used=False,
        ))
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

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='No matching exact code.')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_invalid_exact_task_inside_multi_intent_reports_missing_code(
        self,
        retrieve_mock,
        _,
        router_cls,
    ):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show BAD-CODE details and recommend a hammer.',
            intent=QueryIntent.MULTI_INTENT,
            intents=[QueryIntent.EXACT_LOOKUP, QueryIntent.RECOMMENDATION],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='product_code', value='BAD-CODE')],
            constraints={},
            subqueries=['Show BAD-CODE details.'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find explicit product code BAD-CODE',
                query='Show BAD-CODE details.',
                product_codes=['BAD-CODE'],
            )],
            confidence=0.72,
            memory_used=False,
        ))

        execution = CatalogQueryEngine(self.runtime).execute(
            'Show BAD-CODE details and recommend a hammer.'
        )

        retrieve_mock.assert_not_called()
        self.assertFalse(execution.complete_result)
        self.assertEqual(execution.missing_entities, ['BAD-CODE'])

    @patch('catalog.services.query_engine.AIQueryRouter')
    @patch('catalog.services.query_engine.LLMAnswerer.answer', return_value='No relevant products found in the catalog for your query.')
    @patch.object(CatalogQueryEngine, '_semantic_retrieve')
    def test_invalid_exact_router_code_stays_missing_and_skips_semantic_fallback(
        self,
        retrieve_mock,
        _,
        router_cls,
    ):
        router_cls.return_value.plan.return_value = self._router_result(AIRouterPlan(
            standalone_query='Show NOT-A-CODE details.',
            intent=QueryIntent.EXACT_LOOKUP,
            intents=[QueryIntent.EXACT_LOOKUP],
            scope=QueryScope(source_types=['catalog']),
            entities=[QueryEntity(type='product_code', value='NOT-A-CODE')],
            constraints={},
            subqueries=['Show NOT-A-CODE details.'],
            tasks=[AIRouterTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find explicit product code NOT-A-CODE',
                query='Show NOT-A-CODE details.',
                product_codes=['NOT-A-CODE'],
            )],
            confidence=0.61,
            memory_used=False,
        ))

        execution = CatalogQueryEngine(self.runtime).execute('Show NOT-A-CODE details.')

        retrieve_mock.assert_not_called()
        self.assertEqual(execution.plan.intent, QueryIntent.EXACT_LOOKUP)
        self.assertFalse(execution.complete_result)
        self.assertEqual(execution.total_results, 0)
        self.assertEqual(execution.missing_entities, ['NOT-A-CODE'])

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

    def test_normalizer_does_not_reroute_invalid_exact_task_to_hybrid(self):
        plan = QueryPlan(
            intent=QueryIntent.EXACT_LOOKUP,
            scope=QueryScope(),
            tasks=[QueryTask(
                route=QueryRoute.POSTGRES_EXACT,
                purpose='Find invalid product code',
                query='Show NOT-A-CODE details',
                product_codes=['NOT-A-CODE'],
            )],
            used_model_planner=True,
        )

        normalized = CatalogQueryEngine(self.runtime)._normalize_planned_tasks(plan)

        self.assertEqual(normalized.tasks[0].route, QueryRoute.POSTGRES_EXACT)
        self.assertEqual(normalized.tasks[0].product_codes, ['NOT-A-CODE'])


class CatalogV2ChatMemoryTests(TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(
            openai_api_key='sk-test',
            chat_api_key='sk-test',
            chat_provider='openai',
            chat_model='gpt-4o-mini',
        )

    def test_memory_update_builder_stores_compact_payload(self):
        plan = QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            intents=[QueryIntent.RECOMMENDATION, QueryIntent.GENERAL_SEMANTIC],
            scope=QueryScope(
                catalog_ids=[f'catalog-{index}' for index in range(25)],
                document_ids=[f'document-{index}' for index in range(25)],
                source_types=['catalog'],
            ),
            entities=[
                QueryEntity(type='category', value=f'hammer {index}')
                for index in range(12)
            ],
            subqueries=['Recommend a hammer for masonry'],
        )
        sources = [
            {'name': 'Club Hammer', 'code': 'CHID-1', 'pdf': 'catalog.pdf', 'score': 0.98},
            {'name': 'Club Hammer', 'code': 'CHID-1', 'pdf': 'duplicate.pdf', 'score': 0.97},
            {'name': 'Brass Hammer', 'code': 'BRH-1'},
            {'name': 'Copper Hammer', 'code': 'CPH-1'},
            {'name': 'Sledge Hammer', 'code': 'SLH-1'},
            {'name': 'Ball Pein Hammer', 'code': 'BPH-1'},
            {'name': 'Extra Hammer', 'code': 'EXH-1'},
        ]
        execution = SimpleNamespace(
            plan=plan,
            standalone_query='Recommend a hammer for masonry',
            answer=(
                '<think>internal routing detail</think>\n'
                '# Club Hammer\n'
                '| code | description |\n'
                '```text\n'
                'do not store code block\n'
                '```\n'
                'CHID-1 is a compact hammer for masonry work. ' + ('extra detail ' * 40)
            ),
            sources=sources,
            missing_entities=[],
        )

        turn = build_memory_update('best hammer?', execution)

        self.assertIsNotNone(turn)
        self.assertEqual(turn.last_user_query, 'best hammer?')
        self.assertEqual(turn.last_standalone_query, 'Recommend a hammer for masonry')
        self.assertEqual(turn.last_intent, QueryIntent.RECOMMENDATION)
        self.assertEqual(turn.last_intents, [QueryIntent.RECOMMENDATION, QueryIntent.GENERAL_SEMANTIC])
        self.assertEqual(len(turn.last_entities), 10)
        self.assertEqual(len(turn.last_scope.catalog_ids), 20)
        self.assertEqual(len(turn.last_scope.document_ids), 20)
        self.assertLessEqual(len(turn.last_answer_summary), 240)
        self.assertNotIn('internal routing detail', turn.last_answer_summary)
        self.assertNotIn('do not store code block', turn.last_answer_summary)
        self.assertNotIn('| code |', turn.last_answer_summary)
        self.assertEqual(len(turn.last_sources), 5)
        self.assertEqual(turn.last_sources[0], {'code': 'CHID-1', 'name': 'Club Hammer'})
        self.assertNotIn('pdf', turn.last_sources[0])
        self.assertNotIn('score', turn.last_sources[0])

    def test_memory_update_builder_skips_clarification_turn(self):
        execution = SimpleNamespace(
            plan=QueryPlan(
                intent=QueryIntent.AMBIGUOUS,
                scope=QueryScope(),
                needs_clarification=True,
                clarification_question='Which hammer do you mean?',
            ),
            answer='Which hammer do you mean?',
            sources=[],
            missing_entities=[],
        )

        self.assertIsNone(build_memory_update('hammer', execution))

    def test_memory_update_builder_skips_invalid_missing_turn(self):
        execution = SimpleNamespace(
            plan=QueryPlan(
                intent=QueryIntent.EXACT_LOOKUP,
                scope=QueryScope(),
                entities=[QueryEntity(type='product_code', value='BAD-CODE')],
            ),
            answer='I could not find BAD-CODE.',
            sources=[],
            missing_entities=['BAD-CODE'],
        )

        self.assertIsNone(build_memory_update('show BAD-CODE details', execution))

    @staticmethod
    def _public_payload():
        return {
            'answer': 'CHID answer',
            'sources': [{
                'name': 'Club Hammer',
                'code': 'CHID-1',
                'pdf': 'catalog.pdf',
                'page_start': 4,
                'page_end': 4,
                'catalog': 'Hand Tools',
                'score': 0.98,
            }],
            'query_plan': {
                'intent': 'exact_lookup',
                'intents': ['exact_lookup'],
                'scope': {'catalog_ids': [], 'document_ids': [], 'source_types': ['catalog']},
                'entities': [],
                'constraints': {},
                'subqueries': ['Show CHID-1 details'],
                'tasks': [],
                'needs_clarification': False,
                'clarification_question': '',
                'used_model_planner': False,
            },
            'complete_result': True,
            'total_results': 1,
            'page': 1,
            'page_size': 50,
            'missing_entities': [],
        }

    @staticmethod
    def _admin_payload():
        return {
            'answer': 'Try the brass hammer.',
            'sources': [{
                'name': 'Brass Hammer',
                'code': 'BRH-1',
                'pdf': 'catalog.pdf',
                'page_start': 7,
                'page_end': 7,
                'catalog': 'Hand Tools',
                'score': 0.88,
            }],
            'query_plan': {
                'intent': 'recommendation',
                'intents': ['recommendation'],
                'scope': {'catalog_ids': [], 'document_ids': [], 'source_types': ['catalog']},
                'entities': [],
                'constraints': {},
                'subqueries': ['Recommend a suitable hammer'],
                'tasks': [],
                'needs_clarification': False,
                'clarification_question': '',
                'used_model_planner': False,
            },
            'complete_result': True,
            'total_results': 1,
            'page': 1,
            'page_size': 50,
            'missing_entities': [],
        }

    @patch('catalog.model_config.get_runtime_config')
    @patch('catalog.services.query_engine.CatalogQueryEngine')
    def test_public_chat_reuses_and_saves_k1_memory(self, engine_cls, runtime_mock):
        runtime_mock.return_value = self.runtime
        execution = SimpleNamespace(
            plan=QueryPlan(
                intent=QueryIntent.EXACT_LOOKUP,
                scope=QueryScope(),
                subqueries=['Show CHID-1 details'],
                tasks=[],
            ),
            answer='CHID answer',
            sources=[{
                'name': 'Club Hammer',
                'code': 'CHID-1',
                'pdf': 'catalog.pdf',
                'page_start': 4,
                'page_end': 4,
                'catalog': 'Hand Tools',
                'score': 0.98,
            }],
        )
        execution.as_dict = Mock(return_value=self._public_payload())
        engine_cls.return_value.execute.return_value = execution

        session = self.client.session
        session[PUBLIC_CHAT_MEMORY_KEY] = {
            'last_user_query': 'show CHID-1 details',
            'last_standalone_query': 'Show CHID-1 details',
            'last_intent': 'exact_lookup',
            'last_scope': {'source_types': ['catalog']},
            'last_entities': [{'type': 'product_code', 'value': 'CHID-1'}],
        }
        session.save()

        response = self.client.post(
            '/api/chat/',
            data=json.dumps({
                'query': 'uski variants dikhao',
                'catalog_ids': [],
                'document_ids': [],
                'page': 1,
                'page_size': 50,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        engine_cls.return_value.execute.assert_called_once()
        _, kwargs = engine_cls.return_value.execute.call_args
        self.assertEqual(kwargs['memory']['last_user_query'], 'show CHID-1 details')
        self.assertEqual(kwargs['memory']['last_intent'], 'exact_lookup')
        updated = self.client.session[PUBLIC_CHAT_MEMORY_KEY]
        self.assertEqual(updated['last_user_query'], 'uski variants dikhao')
        self.assertEqual(updated['last_standalone_query'], 'Show CHID-1 details')
        self.assertEqual(updated['last_intent'], 'exact_lookup')
        self.assertEqual(updated['last_sources'][0]['code'], 'CHID-1')

    @patch('catalog.model_config.get_runtime_config')
    @patch('catalog.services.query_engine.CatalogQueryEngine')
    def test_public_chat_clears_stale_memory_for_clarification(self, engine_cls, runtime_mock):
        runtime_mock.return_value = self.runtime
        execution = SimpleNamespace(
            plan=QueryPlan(
                intent=QueryIntent.AMBIGUOUS,
                scope=QueryScope(),
                needs_clarification=True,
                clarification_question='Which product or specification would you like to find?',
            ),
            answer='Which product or specification would you like to find?',
            sources=[],
            missing_entities=[],
        )
        execution.as_dict = Mock(return_value={
            'answer': 'Which product or specification would you like to find?',
            'sources': [],
            'query_plan': {
                'intent': 'ambiguous',
                'intents': [],
                'scope': {'catalog_ids': [], 'document_ids': [], 'source_types': ['catalog']},
                'entities': [],
                'constraints': {},
                'subqueries': [],
                'tasks': [],
                'needs_clarification': True,
                'clarification_question': 'Which product or specification would you like to find?',
                'used_model_planner': True,
            },
            'complete_result': False,
            'total_results': None,
            'page': 1,
            'page_size': 50,
            'missing_entities': [],
        })
        engine_cls.return_value.execute.return_value = execution

        session = self.client.session
        session[PUBLIC_CHAT_MEMORY_KEY] = {
            'last_user_query': 'show CHID-1 details',
            'last_standalone_query': 'Show CHID-1 details',
            'last_intent': 'exact_lookup',
            'last_scope': {'source_types': ['catalog']},
            'last_entities': [{'type': 'product_code', 'value': 'CHID-1'}],
        }
        session.save()

        response = self.client.post(
            '/api/chat/',
            data=json.dumps({
                'query': 'hammer',
                'catalog_ids': [],
                'document_ids': [],
                'page': 1,
                'page_size': 50,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PUBLIC_CHAT_MEMORY_KEY, self.client.session)

    @patch('catalog.model_config.get_runtime_config')
    @patch('catalog.services.query_engine.CatalogQueryEngine')
    def test_admin_chat_saves_memory_under_admin_key(self, engine_cls, runtime_mock):
        runtime_mock.return_value = self.runtime
        execution = SimpleNamespace(
            plan=QueryPlan(
                intent=QueryIntent.RECOMMENDATION,
                scope=QueryScope(source_types=['catalog']),
                subqueries=['Recommend a suitable hammer'],
                tasks=[],
            ),
            answer='Try the brass hammer.',
            sources=[{
                'name': 'Brass Hammer',
                'code': 'BRH-1',
                'pdf': 'catalog.pdf',
                'page_start': 7,
                'page_end': 7,
                'catalog': 'Hand Tools',
                'score': 0.88,
            }],
        )
        execution.as_dict = Mock(return_value=self._admin_payload())
        engine_cls.return_value.execute.return_value = execution

        self.client.force_login(get_user_model().objects.create_user(
            username='chat-admin',
            password='test-password',
            is_staff=True,
        ))
        session = self.client.session
        session['admin_access_token'] = 'admin-token'
        session.save()

        response = self.client.post(
            '/admin-panel/api/chat/',
            data=json.dumps({
                'query': 'best hammer?',
                'catalog_ids': [],
                'document_ids': [],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        engine_cls.return_value.execute.assert_called_once()
        _, kwargs = engine_cls.return_value.execute.call_args
        self.assertIsNone(kwargs['memory'])
        updated = self.client.session[ADMIN_CHAT_MEMORY_KEY]
        self.assertEqual(updated['last_user_query'], 'best hammer?')
        self.assertEqual(updated['last_standalone_query'], 'Recommend a suitable hammer')
        self.assertEqual(updated['last_intent'], 'recommendation')
        self.assertEqual(updated['last_sources'][0]['code'], 'BRH-1')

    @patch('catalog.model_config.get_runtime_config')
    @patch('catalog.services.query_engine.CatalogQueryEngine')
    def test_public_chat_returns_controlled_router_error(self, engine_cls, runtime_mock):
        runtime_mock.return_value = self.runtime
        engine_cls.return_value.execute.side_effect = AIQueryRouterError('router failed')

        response = self.client.post(
            '/api/chat/',
            data=json.dumps({
                'query': 'show CHID-1 details',
                'catalog_ids': [],
                'document_ids': [],
                'page': 1,
                'page_size': 50,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'AI query router failed.')


class CatalogV2PlannerBaselineTests(TestCase):
    def test_deterministic_plan_covers_core_intents(self):
        cases = [
            (
                'compare CHID-1 vs CHID-2',
                QueryIntent.COMPARISON,
                {'codes': ['CHID-1', 'CHID-2']},
            ),
            (
                'How many hammers are available in this catalog?',
                QueryIntent.AGGREGATION,
                {'category_contains': 'hammer'},
            ),
            (
                'Show CHID-1 details',
                QueryIntent.EXACT_LOOKUP,
                {'codes': ['CHID-1']},
            ),
            (
                'List all hammers',
                QueryIntent.EXHAUSTIVE_LIST,
                {'category_contains': 'hammer'},
            ),
            (
                'Best hammer for masonry',
                QueryIntent.RECOMMENDATION,
                {},
            ),
            (
                'Repair this drill',
                QueryIntent.TROUBLESHOOTING,
                {'source_types': ['manual']},
            ),
            (
                'Build a kit with a hammer and wrench',
                QueryIntent.MULTI_INTENT,
                {},
            ),
            (
                'hammer',
                QueryIntent.AMBIGUOUS,
                {'needs_clarification': True},
            ),
            (
                'Summaries all about Cordless Impact Drill Driver.',
                QueryIntent.GENERAL_SEMANTIC,
                {},
            ),
        ]

        for query, expected_intent, expectations in cases:
            with self.subTest(query=query):
                plan = deterministic_plan(query, QueryScope())

                self.assertEqual(plan.intent, expected_intent)
                if 'codes' in expectations:
                    self.assertEqual(
                        [entity.value for entity in plan.entities if entity.type == 'product_code'],
                        expectations['codes'],
                    )
                if 'category_contains' in expectations:
                    category_values = [
                        entity.value for entity in plan.entities if entity.type == 'category'
                    ]
                    self.assertTrue(
                        any(expectations['category_contains'] in value for value in category_values),
                        category_values,
                    )
                if 'source_types' in expectations:
                    self.assertEqual(plan.scope.source_types, expectations['source_types'])
                if 'needs_clarification' in expectations:
                    self.assertEqual(plan.needs_clarification, expectations['needs_clarification'])
                    self.assertTrue(plan.clarification_question)

    def test_query_scope_filters_invalid_source_types(self):
        manual_only = QueryScope(source_types=['catalog', 'manual', 'not-a-real-type'])
        fallback = QueryScope(source_types=['not-a-real-type'])

        self.assertEqual(manual_only.source_types, ['catalog', 'manual'])
        self.assertEqual(fallback.source_types, ['catalog'])

    def test_should_use_model_planner_keeps_complex_queries_on_the_model_path(self):
        query = 'compare CHID-1 vs CHID-2 and recommend the best option'
        plan = deterministic_plan(query, QueryScope())

        self.assertTrue(
            should_use_model_planner(
                query,
                plan,
                valid_product_codes=['CHID-1', 'CHID-2'],
            )
        )


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class CatalogV2FamilyWorkflowTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username='family-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.staff)
        session = self.client.session
        session['admin_access_token'] = 'test-family-token'
        session.save()
        self.catalog = Catalog.objects.create(name='Family Catalog', slug='family-catalog')
        self.document = CatalogDocument.objects.create(
            catalog=self.catalog,
            original_filename='family.pdf',
            file=SimpleUploadedFile('family.pdf', b'%PDF-test'),
            checksum_sha256='6' * 64,
            version=1,
            page_count=3,
            status=CatalogDocument.Status.READY,
            is_active=True,
        )

    def _family(self, source_key: str, product_name: str, *, product_code: str = '', raw_category: str = ''):
        return ProductFamily.objects.create(
            document=self.document,
            source_key=source_key,
            product_name=product_name,
            product_code=product_code,
            raw_category=raw_category,
            review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW,
            page_start=1,
            page_end=1,
        )

    def _chunk(self, ordinal: int, *, family=None, text: str = 'Chunk text'):
        return DocumentChunk.objects.create(
            document=self.document,
            family=family,
            chunk_type=DocumentChunk.ChunkType.PRODUCT_FAMILY,
            text=text,
            page_start=ordinal,
            page_end=ordinal,
            content_hash=hashlib.sha256(f'chunk-{ordinal}-{text}'.encode()).hexdigest(),
            ordinal=ordinal,
            index_status=DocumentChunk.IndexStatus.PENDING,
        )

    def test_list_product_families_returns_family_and_chunk_context(self):
        family = self._family(
            source_key='ui:family-list',
            product_name='Cordless Impact Drill Driver',
            product_code='BLMD-358JST',
            raw_category='Cordless Impact Drill',
        )
        family.aliases = ['impact drill', 'drill driver']
        family.save(update_fields=('aliases',))
        ProductVariant.objects.create(
            family=family,
            name='Standard kit',
            source_row_hash='family-list-variant',
        )
        chunk = self._chunk(1, family=family, text='Cordless Impact Drill chunk')

        response = self.client.get('/admin-panel/api/families/?pdf=family.pdf')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['family_count'], 1)
        self.assertEqual(body['chunk_count'], 1)
        self.assertEqual(body['families'][0]['product_name'], 'Cordless Impact Drill Driver')
        self.assertEqual(body['families'][0]['aliases'], ['impact drill', 'drill driver'])
        self.assertEqual(body['families'][0]['variants'][0]['name'], 'Standard kit')
        self.assertEqual(body['chunks'][0]['family_name'], 'Cordless Impact Drill Driver')
        self.assertEqual(body['chunks'][0]['id'], str(chunk.id))

    def test_save_product_family_replaces_membership_and_cleans_up_empties(self):
        target_family = self._family(
            source_key='ui:target',
            product_name='Old Family',
            product_code='OLD-1',
            raw_category='Old family',
        )
        other_family = self._family(
            source_key='ui:other',
            product_name='Other Family',
            product_code='OTH-1',
            raw_category='Other family',
        )
        chunk_one = self._chunk(1, family=target_family, text='Chunk one')
        chunk_two = self._chunk(2, family=target_family, text='Chunk two')
        chunk_three = self._chunk(3, family=other_family, text='Chunk three')

        response = self.client.post(
            '/admin-panel/api/families/save/',
            data=json.dumps({
                'pdf': 'family.pdf',
                'family_id': str(target_family.id),
                'product_name': 'Cordless Impact Drill Driver',
                'raw_category': 'Cordless Impact Drill',
                'aliases': 'impact drill, cordless drill, drill driver',
                'variants': [
                    {'name': 'Standard kit'},
                    {'name': 'Bare tool'},
                ],
                'review_status': 'approved',
                'chunk_ids': [str(chunk_one.id), str(chunk_three.id)],
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        target_family.refresh_from_db()
        chunk_one.refresh_from_db()
        chunk_two.refresh_from_db()
        chunk_three.refresh_from_db()

        self.assertEqual(target_family.product_name, 'Cordless Impact Drill Driver')
        self.assertEqual(target_family.product_code, '')
        self.assertEqual(target_family.aliases, ['impact drill', 'cordless drill', 'drill driver'])
        self.assertEqual(target_family.variants.count(), 2)
        self.assertEqual(chunk_one.family_id, target_family.id)
        self.assertIsNone(chunk_two.family_id)
        self.assertEqual(chunk_three.family_id, target_family.id)
        self.assertFalse(ProductFamily.objects.filter(id=other_family.id).exists())

        body = response.json()
        self.assertEqual(body['family']['product_name'], 'Cordless Impact Drill Driver')
        self.assertEqual(body['family']['aliases'], ['impact drill', 'cordless drill', 'drill driver'])
        self.assertEqual(len(body['family']['variants']), 2)
        self.assertEqual([variant['name'] for variant in body['family']['variants']], ['Bare tool', 'Standard kit'])
        self.assertEqual(body['family']['chunk_count'], 2)
        self.assertEqual(len(body['families']), 1)

    def test_update_pdf_stage_accepts_families_stage(self):
        response = self.client.post(
            '/admin-panel/api/update-pdf-stage/',
            data=json.dumps({
                'filename': 'family.pdf',
                'stage': 'families',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['stage'], 'families')


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
