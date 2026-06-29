from extractors.mistral_extractor import MistralOCRExtractor, MistralOnlyExtractor
from extractors.deepseek_extractor import DeepSeekOCRExtractor
from extractors.docling_extractor import DoclingExtractor
from extractors.mineru_extractor import MinerUExtractor
from extractors.gemini_extractor import GeminiExtractor
from extractors.llamaparse_extractor import LlamaParseExtractor

_ENGINES = {
    "mistral":      MistralOCRExtractor,
    "mistral-only": MistralOnlyExtractor,
    "deepseek":     DeepSeekOCRExtractor,
    "docling":      DoclingExtractor,
    "mineru":       MinerUExtractor,
    "gemini":       GeminiExtractor,
    "llamaparse":   LlamaParseExtractor,
}

_ENGINE_INFO = {
    "mistral":      "Mistral OCR-4                  (API, accurate, fast)",
    "mistral-only": "Mistral OCR-4 only             (pure Mistral output)",
    "deepseek":     "DeepSeek-OCR via Ollama        (local, vision-based, no API key needed)",
    "docling":      "Docling                        (local, free, no API key needed)",
    "mineru":       "MinerU Cloud API (VLM)         (cloud, 95-96% accuracy)",
    "gemini":       "Gemini 2.0 Flash              (Google's latest multimodal)",
    "llamaparse":   "LlamaParse                     (premium parsing, structured output)",
}


class PDFExtractionRouter:
    def extract(self, pdf_path: str, engine: str = "mistral"):
        extractor_cls = _ENGINES.get(engine)
        if not extractor_cls:
            raise ValueError(f"Unknown OCR engine '{engine}'. Choose: {list(_ENGINES)}")
        return extractor_cls().extract(pdf_path)