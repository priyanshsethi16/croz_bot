"""Pydantic models for product catalog parsing."""
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


class ProductVariant(BaseModel):
    """A product variant (e.g., different sizes/weights)."""
    name: str = ""
    value: str = ""
    unit: str = ""


class OrderingInfo(BaseModel):
    """A single row of ordering/catalog information."""
    cat_no: str = Field(default="", alias="Cat No")
    ord_no: str = Field(default="", alias="Ord No")
    weight: str = Field(default="", alias="Weight")
    dimensions: str = Field(default="", alias="Dimensions")
    extra: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class Product(BaseModel):
    """Full product schema extracted from catalog."""
    product_id: str = ""
    product_name: str = ""
    product_code: str = ""
    page_start: int = 0
    page_end: int = 0
    category: str = ""
    description: str = ""
    features: list[str] = Field(default_factory=list)
    utilities: list[str] = Field(default_factory=list)
    specifications: dict[str, Any] = Field(default_factory=dict)
    variants: list[ProductVariant] = Field(default_factory=list)
    ordering_information: list[dict[str, str]] = Field(default_factory=list)
    children: list[dict[str, Any]] = Field(default_factory=list)
    markdown_chunk: str = ""
    raw_text: str = ""

    @field_validator("product_name", mode="before")
    @classmethod
    def clean_name(cls, v: str) -> str:
        return str(v).strip() if v else ""

    @field_validator("features", "utilities", mode="before")
    @classmethod
    def ensure_list(cls, v: Any) -> list:
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v or []

    def slug(self) -> str:
        """URL-safe slug for filename."""
        import re
        name = self.product_name.lower()
        name = re.sub(r"[^a-z0-9\s]", "", name)
        name = re.sub(r"\s+", "_", name.strip())
        return name[:60] or "unknown_product"


class PageOCRResult(BaseModel):
    """OCR result for a single page."""
    page_num: int
    markdown: str
    success: bool = True
    error: str = ""


class CheckpointData(BaseModel):
    """Checkpoint for resumable processing."""
    pdf_path: str
    total_pages: int
    ocr_completed_pages: list[int] = Field(default_factory=list)
    products_extracted: int = 0
    last_updated: str = ""