"""Structured logging setup."""
import logging
import sys
from pathlib import Path
from datetime import datetime


def setup_logger(name: str = "catalog_parser", log_dir: str = "logs") -> logging.Logger:
    """Configure logger with file + console handlers."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file = Path(log_dir) / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO+
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    # File handler — DEBUG+
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


logger = setup_logger()