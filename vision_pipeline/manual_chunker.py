"""Text-first, page-grounded chunking for English product manuals."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader


CODE_PATTERN = re.compile(
    r'\b(?:[A-Z]{1,12}(?:[-/][A-Z0-9.]+)+|[A-Z]{2,12}-\d[A-Z0-9/-]*)\b'
)
STANDARD_PREFIXES = {'ISO', 'DIN', 'ANSI', 'ASME', 'BIS', 'IEC', 'EN', 'IP'}


class ManualExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManualChunkData:
    title: str
    text: str
    page_start: int
    page_end: int
    ordinal: int
    linked_product_codes: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def _title(text: str, page_number: int) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if 3 <= len(candidate) <= 120:
            return candidate
    return f'Manual page {page_number}'


def _codes(text: str) -> list[str]:
    found = []
    for code in CODE_PATTERN.findall(text.upper()):
        if code.split('-', 1)[0] in STANDARD_PREFIXES:
            continue
        if code not in found:
            found.append(code)
    return found


def chunk_manual_pages(
    pages: list[tuple[int, str]],
    *,
    max_chars: int = 4000,
    overlap_chars: int = 300,
) -> list[ManualChunkData]:
    chunks: list[ManualChunkData] = []
    ordinal = 0
    for page_number, raw_text in pages:
        text = re.sub(r'\n{3,}', '\n\n', str(raw_text or '')).strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            stop = min(len(text), start + max_chars)
            if stop < len(text):
                paragraph_break = text.rfind('\n\n', start, stop)
                if paragraph_break > start + max_chars // 2:
                    stop = paragraph_break
            body = text[start:stop].strip()
            if body:
                chunks.append(ManualChunkData(
                    title=_title(body, page_number),
                    text=body,
                    page_start=page_number,
                    page_end=page_number,
                    ordinal=ordinal,
                    linked_product_codes=_codes(body),
                ))
                ordinal += 1
            if stop >= len(text):
                break
            start = max(stop - overlap_chars, start + 1)
    return chunks


def extract_manual_pdf(pdf_path: str | Path) -> list[ManualChunkData]:
    reader = PdfReader(str(pdf_path))
    pages = [(index, page.extract_text() or '') for index, page in enumerate(reader.pages, 1)]
    chunks = chunk_manual_pages(pages)
    if not chunks:
        raise ManualExtractionError(
            'No extractable manual text was found. This scanned manual requires OCR/VLM review.'
        )
    return chunks
