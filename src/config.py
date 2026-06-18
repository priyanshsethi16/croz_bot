"""Application configuration loaded from .env and config.yaml."""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class OCRConfig:
    mistral_api_key: str = ""
    model: str = "mistral-ocr-latest"
    max_retries: int = 3
    retry_delay: float = 2.0
    timeout: int = 120


@dataclass
class LLMConfig:
    groq_api_key: str = ""
    model: str = "qwen/qwen3-32b"
    temperature: float = 0.1
    max_tokens: int = 1024
    max_retries: int = 3
    retry_delay: float = 2.0


@dataclass
class PathConfig:
    input_pdf: str = ""
    ocr_dir: str = "data/ocr"
    chunks_dir: str = "chunks"
    data_dir: str = "data"
    logs_dir: str = "logs"
    checkpoints_dir: str = "checkpoints"


@dataclass
class ProcessingConfig:
    window_size: int = 4              # Pages per boundary-detection window (2–4)
    overlap_pages: int = 1            # Overlap between sliding windows
    max_prompt_chars: int = 6000      # Max total prompt size (OCR + instructions)
    max_ocr_chars: int = 4500         # Max OCR text chars per LLM call
    boundary_max_tokens: int = 2048   # Max output tokens for boundary pass
    extraction_max_tokens: int = 4096 # Max output tokens for extraction pass (ceiling)
    groq_tpm_limit: int = 6000        # Groq TPM cap for the active tier (free=6000, dev=higher)
    llm_call_delay: float = 0.75      # Seconds between Groq calls (TPM safety)
    min_product_pages: int = 1
    max_product_pages: int = 20
    resume_from_checkpoint: bool = True


@dataclass
class AppConfig:
    ocr: OCRConfig = field(default_factory=OCRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load config from YAML file + override with env vars."""
    cfg = AppConfig()

    # Load YAML if present
    if Path(config_path).exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        if "ocr" in data:
            for k, v in data["ocr"].items():
                if hasattr(cfg.ocr, k):
                    setattr(cfg.ocr, k, v)

        if "llm" in data:
            for k, v in data["llm"].items():
                if hasattr(cfg.llm, k):
                    setattr(cfg.llm, k, v)

        if "paths" in data:
            for k, v in data["paths"].items():
                if hasattr(cfg.paths, k):
                    setattr(cfg.paths, k, v)

        if "processing" in data:
            for k, v in data["processing"].items():
                if hasattr(cfg.processing, k):
                    setattr(cfg.processing, k, v)

    # Env overrides (always win)
    cfg.ocr.mistral_api_key = os.getenv("MISTRAL_API_KEY", cfg.ocr.mistral_api_key)
    cfg.llm.groq_api_key = os.getenv("GROQ_API_KEY", cfg.llm.groq_api_key)
    cfg.llm.model = os.getenv("GROQ_MODEL", cfg.llm.model)
    cfg.ocr.model = os.getenv("MISTRAL_OCR_MODEL", cfg.ocr.model)

    return cfg