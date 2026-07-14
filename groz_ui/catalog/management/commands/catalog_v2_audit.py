"""Read-only readiness audit for the catalog-aware RAG V2 migration."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Audit legacy catalog data and report V2 migration work without creating embeddings."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument(
            "--hash-pdfs",
            action="store_true",
            help="Calculate SHA-256 checksums for uploaded PDFs to identify duplicate files.",
        )

    def handle(self, *args, **options):
        report = {
            "mode": "v2-only",
            "legacy_postgres": self._legacy_postgres_stats(),
            "v2_postgres": self._v2_postgres_stats(),
            "filesystem": self._filesystem_stats(hash_pdfs=options["hash_pdfs"]),
            "qdrant": self._qdrant_stats(),
            "paid_embedding_calls_made": 0,
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return

        pg = report["legacy_postgres"]
        fs = report["filesystem"]
        qd = report["qdrant"]
        v2 = report["v2_postgres"]
        self.stdout.write("Catalog RAG V2 readiness audit (read-only)")
        self.stdout.write(f"  Mode                  : {report['mode']}")
        self.stdout.write(f"  Legacy products       : {pg.get('products', 0)}")
        self.stdout.write(f"  Legacy source values  : {pg.get('source_pdfs', 0)}")
        self.stdout.write(f"  Raw categories        : {pg.get('categories', 0)}")
        self.stdout.write(f"  Uploaded PDFs         : {fs['pdfs']}")
        self.stdout.write(f"  Extracted families    : {fs['product_records']}")
        self.stdout.write(f"  Markdown chunks       : {fs['markdown_chunks']}")
        self.stdout.write(f"  Qdrant collection     : {qd.get('collection', 'unavailable')}")
        self.stdout.write(f"  Qdrant points         : {qd.get('points', 0)}")
        self.stdout.write(f"  V2 documents/families: {v2.get('documents', 0)}/{v2.get('families', 0)}")
        self.stdout.write(f"  V2 review required   : {v2.get('needs_review', 0)}")
        self.stdout.write("  Paid embedding calls  : 0")

        duplicate_groups = fs.get("duplicate_pdf_checksum_groups", [])
        if duplicate_groups:
            self.stdout.write(self.style.WARNING(
                f"  Duplicate PDF groups  : {len(duplicate_groups)}"
            ))

    @staticmethod
    def _legacy_postgres_stats() -> dict:
        table_names = connection.introspection.table_names()
        if "products" not in table_names:
            return {"products": 0, "source_pdfs": 0, "categories": 0, "available": False}

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*),
                       COUNT(DISTINCT NULLIF(source_pdf, '')),
                       COUNT(DISTINCT NULLIF(category, ''))
                FROM products
                """
            )
            products, source_pdfs, categories = cursor.fetchone()
        return {
            "products": products,
            "source_pdfs": source_pdfs,
            "categories": categories,
            "available": True,
        }

    @staticmethod
    def _v2_postgres_stats() -> dict:
        from catalog.models import CatalogDocument, DocumentChunk, ProductFamily

        return {
            'documents': CatalogDocument.objects.count(),
            'active_ready_documents': CatalogDocument.objects.filter(
                status=CatalogDocument.Status.READY,
                is_active=True,
            ).count(),
            'families': ProductFamily.objects.count(),
            'needs_review': ProductFamily.objects.filter(
                review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW,
            ).count(),
            'chunks': DocumentChunk.objects.count(),
            'indexed_chunks': DocumentChunk.objects.filter(
                index_status=DocumentChunk.IndexStatus.INDEXED,
            ).count(),
        }

    @staticmethod
    def _filesystem_stats(*, hash_pdfs: bool) -> dict:
        input_dir = Path(settings.MEDIA_ROOT)
        data_dir = settings.PROJECT_ROOT / "vision_pipeline" / "data"
        pdfs = sorted(input_dir.glob("*.pdf")) if input_dir.exists() else []
        manifests = sorted(data_dir.rglob("products.json")) if data_dir.exists() else []
        markdown_chunks = sorted(data_dir.rglob("chunks/*.md")) if data_dir.exists() else []

        product_records = 0
        invalid_manifests: list[str] = []
        for manifest in manifests:
            try:
                records = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(records, list):
                    product_records += len(records)
                else:
                    invalid_manifests.append(str(manifest))
            except (OSError, json.JSONDecodeError):
                invalid_manifests.append(str(manifest))

        duplicate_groups: list[list[str]] = []
        if hash_pdfs:
            checksums: dict[str, list[str]] = {}
            for pdf in pdfs:
                digest = hashlib.sha256()
                with pdf.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                checksums.setdefault(digest.hexdigest(), []).append(pdf.name)
            duplicate_groups = [names for names in checksums.values() if len(names) > 1]

        source_stems = Counter(manifest.parent.name for manifest in manifests)
        return {
            "pdfs": len(pdfs),
            "manifests": len(manifests),
            "product_records": product_records,
            "markdown_chunks": len(markdown_chunks),
            "invalid_manifests": invalid_manifests,
            "duplicate_manifest_stems": [stem for stem, count in source_stems.items() if count > 1],
            "duplicate_pdf_checksum_groups": duplicate_groups,
        }

    @staticmethod
    def _qdrant_stats() -> dict:
        try:
            from rag_pipeline.providers import COLLECTION, build_qdrant_client

            client = build_qdrant_client()
            if not client.collection_exists(COLLECTION):
                return {"collection": COLLECTION, "points": 0, "available": True}
            points = client.count(collection_name=COLLECTION, exact=True).count
            return {
                "collection": COLLECTION,
                "points": points,
                "available": True,
            }
        except Exception as exc:  # audit must still report PostgreSQL/filesystem state
            return {
                "collection": "unavailable",
                "points": 0,
                "available": False,
                "error": type(exc).__name__,
            }
