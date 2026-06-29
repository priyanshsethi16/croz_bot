import uuid
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


def catalog_document_upload_path(instance, filename: str) -> str:
    """Store uploads under stable IDs; never trust a client-supplied path."""
    safe_name = Path(filename).name
    return f"catalog_documents/{instance.catalog_id}/{instance.id}/{safe_name}"


class ApiKey(models.Model):
    name       = models.CharField(max_length=64, unique=True)
    value      = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class ModelConfiguration(models.Model):
    """Singleton configuration for catalog AI providers and model routing."""

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    embedding_model = models.CharField(max_length=64, default='text-embedding-3-small', editable=False)
    vision_model = models.CharField(max_length=80, default='gemini-2.5-flash')
    chat_provider = models.CharField(max_length=16, default='gemini')
    chat_model = models.CharField(max_length=80, default='gemini-2.5-flash')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_model_configurations',
    )

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return 'Catalog model configuration'


class Catalog(models.Model):
    """A logical GROZ catalog that can have multiple uploaded versions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    brand = models.CharField(max_length=120, default='GROZ', blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class CatalogDocument(models.Model):
    """One immutable catalog/manual PDF version and its processing state."""

    class SourceType(models.TextChoices):
        CATALOG = 'catalog', 'Catalog'
        MANUAL = 'manual', 'Manual'

    class Status(models.TextChoices):
        UPLOADED = 'uploaded', 'Uploaded'
        EXTRACTING = 'extracting', 'Extracting'
        REVIEW = 'review', 'Needs review'
        INDEXING = 'indexing', 'Indexing'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'
        ARCHIVED = 'archived', 'Archived'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    catalog = models.ForeignKey(Catalog, on_delete=models.PROTECT, related_name='documents')
    source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.CATALOG)
    original_filename = models.CharField(max_length=255)
    file = models.FileField(upload_to=catalog_document_upload_path, max_length=500)
    checksum_sha256 = models.CharField(max_length=64, unique=True)
    version = models.PositiveIntegerField(default=1)
    page_count = models.PositiveIntegerField(default=0)
    extraction_schema_version = models.PositiveSmallIntegerField(default=2)
    source_schema = models.CharField(max_length=32, default='v2')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UPLOADED)
    is_active = models.BooleanField(default=False)
    failure_summary = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='uploaded_catalog_documents',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('catalog_id', '-version')
        constraints = [
            models.UniqueConstraint(
                fields=('catalog', 'version'),
                name='catalog_document_unique_version',
            ),
            models.UniqueConstraint(
                fields=('catalog',),
                condition=Q(is_active=True),
                name='catalog_document_one_active_version',
            ),
        ]
        indexes = [
            models.Index(fields=('status', 'is_active'), name='catalog_doc_status_idx'),
            models.Index(fields=('source_type', 'is_active'), name='catalog_doc_source_idx'),
        ]

    def __str__(self):
        return f'{self.catalog.name} v{self.version} ({self.source_type})'


class IngestionJob(models.Model):
    """Durable orchestration state for extract/validate/index work."""

    class Stage(models.TextChoices):
        UPLOAD = 'upload', 'Upload'
        RASTERIZE = 'rasterize', 'Rasterize'
        EXTRACT = 'extract', 'Extract'
        ASSEMBLE = 'assemble', 'Assemble'
        VALIDATE = 'validate', 'Validate'
        INDEX = 'index', 'Index'
        RECONCILE = 'reconcile', 'Reconcile'
        COMPLETE = 'complete', 'Complete'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(CatalogDocument, on_delete=models.CASCADE, related_name='jobs')
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.UPLOAD)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    completed_units = models.PositiveIntegerField(default=0)
    total_units = models.PositiveIntegerField(default=0)
    retry_count = models.PositiveSmallIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    error_summary = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('status', 'stage'), name='ingestion_job_state_idx'),
            models.Index(fields=('document', '-created_at'), name='ingestion_job_doc_idx'),
        ]

    def __str__(self):
        return f'{self.document_id}: {self.stage}/{self.status}'


class ExtractionPage(models.Model):
    """Page-level checkpoint that distinguishes empty pages from failures."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        EXTRACTED = 'extracted', 'Extracted'
        EMPTY = 'empty', 'Empty'
        FAILED = 'failed', 'Failed'

    id = models.BigAutoField(primary_key=True)
    document = models.ForeignKey(CatalogDocument, on_delete=models.CASCADE, related_name='extraction_pages')
    page_number = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    image_path = models.CharField(max_length=500, blank=True)
    extraction_result = models.JSONField(default=list, blank=True)
    error_summary = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('document_id', 'page_number')
        constraints = [
            models.UniqueConstraint(
                fields=('document', 'page_number'),
                name='extraction_page_unique_number',
            ),
        ]
        indexes = [
            models.Index(fields=('document', 'status'), name='extract_page_state_idx'),
        ]


class Category(models.Model):
    """Reviewed English taxonomy used for exhaustive catalog queries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=160, unique=True)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='children')
    aliases = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class ProductFamily(models.Model):
    """Validated product family assembled from one or more source pages."""

    class ReviewStatus(models.TextChoices):
        APPROVED = 'approved', 'Approved'
        NEEDS_REVIEW = 'needs_review', 'Needs review'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(CatalogDocument, on_delete=models.CASCADE, related_name='product_families')
    source_key = models.CharField(max_length=255)
    product_name = models.CharField(max_length=500)
    product_code = models.CharField(max_length=160, blank=True)
    raw_category = models.CharField(max_length=500, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    normalized_category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='product_families',
    )
    description = models.TextField(blank=True)
    features = models.JSONField(default=list, blank=True)
    utilities = models.JSONField(default=list, blank=True)
    materials = models.JSONField(default=dict, blank=True)
    specifications = models.JSONField(default=dict, blank=True)
    page_start = models.PositiveIntegerField(default=0)
    page_end = models.PositiveIntegerField(default=0)
    extraction_confidence = models.FloatField(
        default=0.0,
        validators=(MinValueValidator(0.0), MaxValueValidator(1.0)),
    )
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.NEEDS_REVIEW,
    )
    raw_extraction = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('document_id', 'page_start', 'product_name')
        constraints = [
            models.UniqueConstraint(
                fields=('document', 'source_key'),
                name='product_family_unique_source',
            ),
        ]
        indexes = [
            models.Index(fields=('document', 'review_status'), name='product_family_review_idx'),
            models.Index(fields=('product_code',), name='product_family_code_idx'),
            models.Index(fields=('normalized_category',), name='product_family_cat_idx'),
        ]

    def __str__(self):
        return self.product_name


class ProductVariant(models.Model):
    """One orderable child/variant of a product family."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(ProductFamily, on_delete=models.CASCADE, related_name='variants')
    product_code = models.CharField(max_length=160, blank=True)
    order_number = models.CharField(max_length=160, blank=True)
    name = models.CharField(max_length=500, blank=True)
    size = models.CharField(max_length=255, blank=True)
    unit = models.CharField(max_length=80, blank=True)
    specifications = models.JSONField(default=dict, blank=True)
    ordering_data = models.JSONField(default=dict, blank=True)
    page_start = models.PositiveIntegerField(default=0)
    page_end = models.PositiveIntegerField(default=0)
    source_row_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('family_id', 'product_code', 'order_number', 'name')
        constraints = [
            models.UniqueConstraint(
                fields=('family', 'source_row_hash'),
                name='product_variant_unique_row',
            ),
        ]
        indexes = [
            models.Index(fields=('product_code',), name='product_variant_code_idx'),
            models.Index(fields=('order_number',), name='product_variant_order_idx'),
        ]

    def __str__(self):
        return self.product_code or self.order_number or self.name or str(self.id)


class DocumentChunk(models.Model):
    """Grounded text unit and its source-aware Qdrant indexing state."""

    class ChunkType(models.TextChoices):
        PRODUCT_FAMILY = 'product_family', 'Product family'
        PRODUCT_VARIANT = 'product_variant', 'Product variant'
        MANUAL_SECTION = 'manual_section', 'Manual section'

    class IndexStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        INDEXED = 'indexed', 'Indexed'
        FAILED = 'failed', 'Failed'
        STALE = 'stale', 'Stale'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(CatalogDocument, on_delete=models.CASCADE, related_name='chunks')
    family = models.ForeignKey(
        ProductFamily,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='chunks',
    )
    variant = models.ForeignKey(
        ProductVariant,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='chunks',
    )
    chunk_type = models.CharField(max_length=24, choices=ChunkType.choices)
    text = models.TextField()
    page_start = models.PositiveIntegerField(default=0)
    page_end = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64)
    ordinal = models.PositiveIntegerField(default=0)
    qdrant_point_id = models.UUIDField(null=True, blank=True, unique=True)
    index_status = models.CharField(
        max_length=12,
        choices=IndexStatus.choices,
        default=IndexStatus.PENDING,
    )
    embedding_model = models.CharField(max_length=80, default='text-embedding-3-small')
    embedding_dimensions = models.PositiveSmallIntegerField(default=1536)
    extraction_schema_version = models.PositiveSmallIntegerField(default=2)
    linked_product_codes = models.JSONField(default=list, blank=True)
    index_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('document_id', 'page_start', 'ordinal')
        constraints = [
            models.UniqueConstraint(
                fields=('document', 'content_hash', 'ordinal'),
                name='document_chunk_unique_content',
            ),
        ]
        indexes = [
            models.Index(fields=('document', 'index_status'), name='document_chunk_state_idx'),
            models.Index(fields=('chunk_type', 'index_status'), name='document_chunk_type_idx'),
        ]

    def __str__(self):
        return f'{self.document_id}:{self.chunk_type}:{self.ordinal}'
