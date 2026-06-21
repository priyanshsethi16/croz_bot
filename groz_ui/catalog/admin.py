from django.contrib import admin

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


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ('name', 'updated_at')
    readonly_fields = ('value', 'updated_at')


@admin.register(ModelConfiguration)
class ModelConfigurationAdmin(admin.ModelAdmin):
    list_display = ('embedding_model', 'vision_model', 'chat_provider', 'chat_model', 'updated_at')
    readonly_fields = ('embedding_model', 'updated_at', 'updated_by')


@admin.register(Catalog)
class CatalogAdmin(admin.ModelAdmin):
    list_display = ('name', 'brand', 'slug', 'updated_at')
    search_fields = ('name', 'brand', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(CatalogDocument)
class CatalogDocumentAdmin(admin.ModelAdmin):
    list_display = ('original_filename', 'catalog', 'version', 'source_type', 'status', 'is_active', 'updated_at')
    list_filter = ('source_type', 'status', 'is_active')
    search_fields = ('original_filename', 'checksum_sha256', 'catalog__name')
    readonly_fields = ('checksum_sha256', 'status', 'is_active', 'created_at', 'updated_at')


@admin.register(IngestionJob)
class IngestionJobAdmin(admin.ModelAdmin):
    list_display = ('document', 'stage', 'status', 'completed_units', 'total_units', 'retry_count', 'updated_at')
    list_filter = ('stage', 'status')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'completed_at')


@admin.register(ExtractionPage)
class ExtractionPageAdmin(admin.ModelAdmin):
    list_display = ('document', 'page_number', 'status', 'attempts', 'updated_at')
    list_filter = ('status',)
    search_fields = ('document__original_filename',)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'parent', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(ProductFamily)
class ProductFamilyAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'product_code', 'document', 'normalized_category', 'review_status', 'page_start')
    list_filter = ('review_status', 'normalized_category')
    search_fields = ('product_name', 'product_code', 'raw_category')
    actions = ('approve_selected_products', 'reject_selected_products')

    @admin.action(description='Approve selected products with reviewed categories')
    def approve_selected_products(self, request, queryset):
        eligible = queryset.filter(normalized_category__isnull=False).exclude(product_name__istartswith='Unknown Product')
        document_ids = list(eligible.values_list('document_id', flat=True).distinct())
        updated = eligible.update(review_status=ProductFamily.ReviewStatus.APPROVED)

        for document_id in document_ids:
            remaining = ProductFamily.objects.filter(document_id=document_id).exclude(
                review_status=ProductFamily.ReviewStatus.APPROVED
            ).exists()
            if not remaining:
                CatalogDocument.objects.filter(
                    id=document_id,
                    status=CatalogDocument.Status.REVIEW,
                ).update(status=CatalogDocument.Status.INDEXING)
        skipped = queryset.count() - updated
        self.message_user(
            request,
            f'Approved {updated} product(s); skipped {skipped} without a reviewed category/name.',
        )

    @admin.action(description='Reject selected extracted products')
    def reject_selected_products(self, request, queryset):
        updated = queryset.update(review_status=ProductFamily.ReviewStatus.REJECTED)
        self.message_user(request, f'Rejected {updated} product(s).')


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ('product_code', 'order_number', 'name', 'family')
    search_fields = ('product_code', 'order_number', 'name', 'family__product_name')


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ('document', 'chunk_type', 'page_start', 'index_status', 'qdrant_point_id')
    list_filter = ('chunk_type', 'index_status')
    search_fields = ('family__product_name', 'family__product_code', 'content_hash')
    readonly_fields = ('content_hash', 'qdrant_point_id', 'created_at', 'updated_at')
