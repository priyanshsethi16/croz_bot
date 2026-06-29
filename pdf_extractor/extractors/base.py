from abc import ABC, abstractmethod
from pydantic import BaseModel
from typing import Any, Optional


class ExtractionResult(BaseModel):
    engine: str
    text: str = ""
    markdown: str = ""
    html: str = ""
    json_data: Optional[Any] = None
    success: bool = True
    error: Optional[str] = None


class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, pdf_path: str) -> ExtractionResult:
        pass