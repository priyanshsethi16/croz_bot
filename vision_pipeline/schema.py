"""Validated, backward-compatible schema for page-level VLM extraction."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SCHEMA_VERSION = 2


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra='allow')

    page_number: int | None = None
    catalog_section: str = ''
    brand: str = ''


class ProductChild(BaseModel):
    model_config = ConfigDict(extra='allow')

    product_code: str = ''
    product_name: str = ''
    description: str = ''
    features: list[Any] = Field(default_factory=list)
    utilities: list[Any] = Field(default_factory=list)
    materials: dict[str, Any] = Field(default_factory=dict)
    specifications: dict[str, Any] = Field(default_factory=dict)
    color: str = ''
    sizes: list[dict[str, Any]] = Field(default_factory=list)
    ordering_table: list[dict[str, Any]] = Field(default_factory=list)
    accessories_included: list[Any] = Field(default_factory=list)
    notes: str = ''

    @field_validator('features', 'utilities', 'sizes', 'ordering_table', 'accessories_included', mode='before')
    @classmethod
    def list_or_empty(cls, value):
        return value if isinstance(value, list) else []

    @field_validator('materials', 'specifications', mode='before')
    @classmethod
    def dict_or_empty(cls, value):
        return value if isinstance(value, dict) else {}


class ProductExtraction(BaseModel):
    """A product family extracted from one original source page."""

    model_config = ConfigDict(extra='allow')

    schema_version: int = SCHEMA_VERSION
    document_id: str = ''
    source_pdf: str = ''
    page_num: int = 0
    original_page_num: int = 0
    page_metadata: PageMetadata = Field(default_factory=PageMetadata)
    product_name: str
    product_code: str = ''
    aliases: list[str] = Field(default_factory=list)
    category: str = ''
    raw_category: str = ''
    normalized_category_suggestion: str = ''
    sub_category: str = ''
    description: str = ''
    features: list[Any] = Field(default_factory=list)
    utilities: list[Any] = Field(default_factory=list)
    materials: dict[str, Any] = Field(default_factory=dict)
    specifications: dict[str, Any] = Field(default_factory=dict)
    certifications: list[Any] = Field(default_factory=list)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    safety_warnings: list[Any] = Field(default_factory=list)
    warranty: str = ''
    country_of_origin: str = ''
    packaging: dict[str, Any] = Field(default_factory=dict)
    notes: str = ''
    raw_text_blocks: list[Any] = Field(default_factory=list)
    children: list[ProductChild] = Field(default_factory=list)
    extraction_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator(
        'features',
        'utilities',
        'certifications',
        'safety_warnings',
        'raw_text_blocks',
        'aliases',
        mode='before',
    )
    @classmethod
    def list_or_empty(cls, value):
        return value if isinstance(value, list) else []

    @field_validator('materials', 'specifications', 'compatibility', 'packaging', mode='before')
    @classmethod
    def dict_or_empty(cls, value):
        return value if isinstance(value, dict) else {}

    @field_validator('product_name', mode='before')
    @classmethod
    def require_product_name(cls, value):
        value = str(value or '').strip()
        if not value:
            raise ValueError('product_name is required')
        return value

    @model_validator(mode='after')
    def fill_compatible_source_fields(self):
        if not self.raw_category:
            self.raw_category = self.category or self.page_metadata.catalog_section
        if not self.category:
            self.category = self.raw_category
        if not self.original_page_num:
            self.original_page_num = self.page_num
        if self.page_metadata.page_number is None:
            self.page_metadata.page_number = self.original_page_num
        return self


class ExtractionValidationIssue(BaseModel):
    item_index: int
    error: str


def validate_page_products(
    raw_products: Any,
    *,
    page_num: int,
    page_offset: int = 0,
    source_pdf: str = '',
    document_id: str = '',
) -> tuple[list[ProductExtraction], list[ExtractionValidationIssue]]:
    """Validate a VLM page response while preserving valid siblings."""
    if not isinstance(raw_products, list):
        return [], [ExtractionValidationIssue(item_index=-1, error='VLM response is not a JSON array')]

    validated: list[ProductExtraction] = []
    issues: list[ExtractionValidationIssue] = []
    original_page_num = page_num + page_offset

    for index, raw in enumerate(raw_products):
        if not isinstance(raw, dict):
            issues.append(ExtractionValidationIssue(item_index=index, error='Product entry is not an object'))
            continue
        candidate = {
            **raw,
            'schema_version': SCHEMA_VERSION,
            'document_id': document_id,
            'source_pdf': source_pdf,
            'page_num': page_num,
            'original_page_num': original_page_num,
        }
        try:
            validated.append(ProductExtraction.model_validate(candidate))
        except ValidationError as exc:
            issues.append(ExtractionValidationIssue(item_index=index, error=str(exc)))

    return validated, issues
