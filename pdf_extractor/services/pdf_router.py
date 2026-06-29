from extractors.mistral_extractor import MistralOCRExtractor, MistralOnlyExtractor
from extractors.deepseek_extractor import DeepSeekOCRExtractor
from extractors.docling_extractor import DoclingExtractor

_ENGINES = {
    "mistral":      MistralOCRExtractor,
    "mistral-only": MistralOnlyExtractor,
    "deepseek":     DeepSeekOCRExtractor,
    "docling":      DoclingExtractor,
}

_ENGINE_INFO = {
    "mistral":      "Mistral OCR-4 only             (pure Mistral output)",
    "mistral-only": "Mistral OCR-4 only             (pure Mistral output)",
    "deepseek":     "DeepSeek-OCR via Ollama        (local, vision-based, no API key needed)",
    "docling":      "Docling                        (local, free, no API key needed)",
}


class PDFExtractionRouter:
    def extract(self, pdf_path: str, engine: str = "mistral"):
        extractor_cls = _ENGINES.get(engine)
        if not extractor_cls:
            raise ValueError(f"Unknown OCR engine '{engine}'. Choose: {list(_ENGINES)}")
        return extractor_cls().extract(pdf_path)