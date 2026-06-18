"""
Product boundary detection via Qwen (Groq).

Architecture:
  OCR pages → sliding 4-page windows → Qwen boundary pass → merged families
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from groq import Groq

from src.config import LLMConfig, ProcessingConfig
from src.models.product import PageOCRResult
from src.utils.llm_utils import call_groq, repair_json, truncate_text
from src.utils.logger import logger


@dataclass
class ProductBoundary:
    """A detected product family spanning one or more pages."""
    page_start: int
    page_end: int
    candidate_title: str = ""
    candidate_code: str = ""
    confidence: float = 0.0
    pages_markdown: list[str] = field(default_factory=list)

    def combined_text(self) -> str:
        return "\n\n---PAGE BREAK---\n\n".join(self.pages_markdown)

    def to_dict(self) -> dict:
        return {
            "page_start": self.page_start,
            "page_end": self.page_end,
            "candidate_title": self.candidate_title,
            "candidate_code": self.candidate_code,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict, page_map: dict[int, str]) -> "ProductBoundary":
        ps = int(data["page_start"])
        pe = int(data["page_end"])
        pages_md = [page_map.get(p, "") for p in range(ps, pe + 1)]
        return cls(
            page_start=ps,
            page_end=pe,
            candidate_title=data.get("candidate_title", data.get("product_name", "")),
            candidate_code=data.get("candidate_code", data.get("product_code", "")),
            confidence=float(data.get("confidence", 0.0)),
            pages_markdown=pages_md,
        )


@dataclass
class _BoundaryCandidate:
    product_name: str
    product_code: str
    page_start: int
    page_end: int
    confidence: float = 0.0


BOUNDARY_PROMPT = """You analyze OCR text from an industrial product catalog (e.g. Groz tools).
Identify ALL distinct product FAMILIES visible in the OCR below.

Page range for this window: {page_start} to {page_end}

OCR TEXT (pages marked with === PAGE N ===):
{text}

Return ONLY a JSON array. Each element:
{{
  "product_name": "full family name",
  "product_code": "primary code e.g. BPID, CHID — empty string if unknown",
  "page_start": <int>,
  "page_end": <int>,
  "confidence": <float 0.0-1.0>
}}

Rules:
- Multiple product families may appear on ONE page — return one entry per family
- Group child variants under ONE family (e.g. "Ball Pein Hammers" with BPID and BPID-SF = one entry, code=BPID)
- Products spanning multiple pages = one entry with correct page_start/page_end
- Section labels (Features, Specifications, SIZES, ANSI, Utility, Ordering) are NOT products
- page_start and page_end MUST be integers within [{page_start}, {page_end}]
- Return valid JSON only — no markdown fences, no explanation
"""

# ── Cover / index page skip patterns ─────────────────────────────────────────

# Pages whose entire heading content matches these terms are skipped as
# non-product pages (covers, tables of contents, section dividers).
_COVER_INDEX_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*table\s+of\s+contents?\s*$", re.I),
    re.compile(r"^\s*contents?\s*$", re.I),
    re.compile(r"^\s*index\s*$", re.I),
    re.compile(r"^\s*section\s+\d+", re.I),
    re.compile(r"^\s*introduction\s*$", re.I),
    re.compile(r"^\s*foreword\s*$", re.I),
    re.compile(r"^\s*preface\s*$", re.I),
]


def _is_cover_or_index_page(markdown: str) -> bool:
    """Return True if the page looks like a cover, TOC, or section-divider."""
    # Extract all heading lines
    heading_re = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)
    headings = [m.group(1).strip() for m in heading_re.finditer(markdown)]

    # If there are no headings at all, check if first non-empty line matches
    if not headings:
        lines = [l.strip() for l in markdown.splitlines() if l.strip()]
        if lines:
            headings = [lines[0]]

    if not headings:
        return False

    # All headings must match a cover/index pattern for the page to be skipped
    return all(
        any(pat.match(h) for pat in _COVER_INDEX_PATTERNS)
        for h in headings
    )


def _is_table_orphan(markdown: str, has_heading: bool) -> bool:
    """
    Return True when a page starts mid-table and has no product heading of
    its own — it's a continuation of the previous product's ordering table.

    Criteria:
      - First non-empty content line contains '|' (table row)
      - No top-level product heading (# or ##) found on the page
    """
    if has_heading:
        return False

    # Look at the first few non-empty, non-separator lines
    lines = [l.strip() for l in markdown.splitlines() if l.strip()]
    if not lines:
        return False

    # The page starts with a pipe — almost certainly a continuation table row
    return lines[0].startswith("|")


class QwenBoundaryDetector:
    """Detect product family boundaries using Qwen via Groq."""

    def __init__(self, llm_config: LLMConfig, processing_config: ProcessingConfig):
        self.llm_config = llm_config
        self.processing = processing_config
        if not llm_config.groq_api_key:
            raise ValueError("GROQ_API_KEY is not set.")
        self.client = Groq(api_key=llm_config.groq_api_key)

    def _format_window_text(
        self,
        page_nums: list[int],
        page_map: dict[int, str],
        max_chars: int,
    ) -> str:
        """Build window OCR text with page markers, respecting char budget."""
        parts: list[str] = []
        used = 0
        separator = "\n\n"

        for page_num in page_nums:
            header = f"=== PAGE {page_num} ==="
            body = page_map.get(page_num, "")
            block = f"{header}\n{body}"
            block_len = len(block) + (len(separator) if parts else 0)

            if parts and used + block_len > max_chars:
                break
            if not parts and len(block) > max_chars:
                block = truncate_text(block, max_chars)
            parts.append(block)
            used += block_len

        return separator.join(parts)

    def _detect_window(
        self,
        page_nums: list[int],
        page_map: dict[int, str],
    ) -> list[_BoundaryCandidate]:
        if not page_nums:
            return []

        page_start = page_nums[0]
        page_end = page_nums[-1]
        max_ocr_chars = self.processing.max_ocr_chars
        ocr_text = self._format_window_text(page_nums, page_map, max_ocr_chars)

        prompt = BOUNDARY_PROMPT.format(
            text=ocr_text,
            page_start=page_start,
            page_end=page_end,
        )

        if len(prompt) > self.processing.max_prompt_chars:
            overflow = len(prompt) - self.processing.max_prompt_chars
            ocr_text = truncate_text(ocr_text, max(500, len(ocr_text) - overflow))
            prompt = BOUNDARY_PROMPT.format(
                text=ocr_text,
                page_start=page_start,
                page_end=page_end,
            )
            logger.warning(
                f"Boundary prompt trimmed for pages {page_start}-{page_end} "
                f"({len(prompt)} chars)"
            )

        raw = call_groq(
            self.client,
            self.llm_config,
            prompt,
            max_tokens=self.processing.boundary_max_tokens,
            inter_call_delay=self.processing.llm_call_delay,
        )

        try:
            parsed = repair_json(raw, expect_array=True)
        except json.JSONDecodeError as exc:
            logger.warning(
                f"Boundary JSON repair failed for pages {page_start}-{page_end}: {exc}"
            )
            return []

        if not isinstance(parsed, list):
            logger.warning(f"Boundary response not a list for pages {page_start}-{page_end}")
            return []

        candidates: list[_BoundaryCandidate] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                ps = int(item.get("page_start", page_start))
                pe = int(item.get("page_end", page_end))
            except (TypeError, ValueError):
                continue

            ps = max(page_start, min(ps, page_end))
            pe = max(ps, min(pe, page_end))

            candidates.append(
                _BoundaryCandidate(
                    product_name=str(item.get("product_name", "")).strip(),
                    product_code=str(item.get("product_code", "")).strip().upper(),
                    page_start=ps,
                    page_end=pe,
                    confidence=float(item.get("confidence", 0.5)),
                )
            )

        logger.debug(
            f"Window pages {page_start}-{page_end}: {len(candidates)} families detected"
        )
        return candidates

    def detect(self, ocr_results: list[PageOCRResult]) -> list[ProductBoundary]:
        if not ocr_results:
            return []

        page_map = {r.page_num: r.markdown for r in ocr_results}
        page_nums = sorted(page_map.keys())

        # Filter out cover/index pages before windowing
        filtered_page_nums: list[int] = []
        for pn in page_nums:
            md = page_map.get(pn, "")
            if _is_cover_or_index_page(md):
                logger.debug(f"Page {pn}: skipped (cover/index/section-divider)")
            else:
                filtered_page_nums.append(pn)

        # Apply table-orphan rule: extend the previous boundary to absorb
        # continuation pages that start mid-table with no heading.
        heading_re = re.compile(r"^#{1,2}\s+\S", re.MULTILINE)
        orphan_pages: set[int] = set()
        for idx, pn in enumerate(filtered_page_nums):
            md = page_map.get(pn, "")
            has_heading = bool(heading_re.search(md))
            if _is_table_orphan(md, has_heading):
                orphan_pages.add(pn)
                logger.debug(f"Page {pn}: table-orphan (continuation of previous product)")

        # Only run boundary detection on non-orphan pages
        detection_pages = [p for p in filtered_page_nums if p not in orphan_pages]

        # Window size bumped to 4 (clamped to available pages)
        window_size = max(2, min(4, self.processing.window_size))
        stride = max(1, window_size - self.processing.overlap_pages)

        all_candidates: list[_BoundaryCandidate] = []
        i = 0
        while i < len(detection_pages):
            window_pages = detection_pages[i : i + window_size]
            if window_pages:
                candidates = self._detect_window(window_pages, page_map)
                all_candidates.extend(candidates)
            if i + window_size >= len(detection_pages):
                break
            i += stride

        merged = _merge_candidates(all_candidates)

        # Extend boundary page_end to absorb any orphan pages that immediately
        # follow each merged boundary.
        if orphan_pages:
            merged = _absorb_orphan_pages(merged, orphan_pages, page_map)

        boundaries = _candidates_to_boundaries(merged, page_map)

        logger.info(
            f"Qwen boundary detection: {len(all_candidates)} raw → "
            f"{len(merged)} merged → {len(boundaries)} product families"
        )
        return boundaries


# ── Orphan-page absorption ────────────────────────────────────────────────────


def _absorb_orphan_pages(
    merged: list[_BoundaryCandidate],
    orphan_pages: set[int],
    page_map: dict[int, str],
) -> list[_BoundaryCandidate]:
    """
    For each orphan page, extend the nearest preceding boundary's page_end
    so the continuation rows are included in that product's raw text.
    """
    if not merged:
        return merged

    for orphan_pn in sorted(orphan_pages):
        # Find the last boundary whose page_end < orphan_pn
        best_idx: int | None = None
        for idx, cand in enumerate(merged):
            if cand.page_end < orphan_pn:
                best_idx = idx
        if best_idx is not None:
            cand = merged[best_idx]
            if cand.page_end < orphan_pn:
                merged[best_idx] = _BoundaryCandidate(
                    product_name=cand.product_name,
                    product_code=cand.product_code,
                    page_start=cand.page_start,
                    page_end=orphan_pn,
                    confidence=cand.confidence,
                )

    return sorted(merged, key=lambda c: (c.page_start, c.page_end, c.product_name))


# ── Merge & fallback helpers ─────────────────────────────────────────────────


def _normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9\-]", "", code.upper().strip())


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def _names_similar(a: str, b: str) -> bool:
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    tokens_a = {t for t in na.split() if len(t) > 3}
    tokens_b = {t for t in nb.split() if len(t) > 3}
    return bool(tokens_a & tokens_b)


def _pages_overlap(a: _BoundaryCandidate, b: _BoundaryCandidate) -> bool:
    return not (a.page_end < b.page_start or b.page_end < a.page_start)


def _merge_two(a: _BoundaryCandidate, b: _BoundaryCandidate) -> _BoundaryCandidate:
    better = a if a.confidence >= b.confidence else b
    return _BoundaryCandidate(
        product_name=better.product_name or a.product_name or b.product_name,
        product_code=better.product_code or a.product_code or b.product_code,
        page_start=min(a.page_start, b.page_start),
        page_end=max(a.page_end, b.page_end),
        confidence=max(a.confidence, b.confidence),
    )


def _merge_candidates(candidates: list[_BoundaryCandidate]) -> list[_BoundaryCandidate]:
    if not candidates:
        return []

    sorted_cands = sorted(
        candidates,
        key=lambda c: (c.page_start, c.page_end, -c.confidence),
    )
    merged: list[_BoundaryCandidate] = []

    for cand in sorted_cands:
        if not cand.product_name and not cand.product_code:
            continue

        absorbed = False
        for idx, existing in enumerate(merged):
            same_code = (
                _normalize_code(cand.product_code)
                and _normalize_code(cand.product_code) == _normalize_code(existing.product_code)
            )
            if same_code or (
                _pages_overlap(cand, existing)
                and _names_similar(cand.product_name, existing.product_name)
            ):
                merged[idx] = _merge_two(existing, cand)
                absorbed = True
                break

        if not absorbed:
            merged.append(cand)

    return sorted(merged, key=lambda c: (c.page_start, c.page_end, c.product_name))


def _candidates_to_boundaries(
    candidates: list[_BoundaryCandidate],
    page_map: dict[int, str],
) -> list[ProductBoundary]:
    boundaries: list[ProductBoundary] = []
    for cand in candidates:
        pages_md = [page_map.get(p, "") for p in range(cand.page_start, cand.page_end + 1)]
        boundaries.append(
            ProductBoundary(
                page_start=cand.page_start,
                page_end=cand.page_end,
                candidate_title=cand.product_name,
                candidate_code=cand.product_code,
                confidence=cand.confidence,
                pages_markdown=pages_md,
            )
        )
    return boundaries


def _detect_boundaries_heuristic(ocr_results: list[PageOCRResult]) -> list[ProductBoundary]:
    """
    Non-LLM fallback: split on top-level product headings within each page.
    Used only with --rules-only.
    """
    if not ocr_results:
        return []

    page_map = {r.page_num: r.markdown for r in ocr_results}
    heading_re = re.compile(r"^#{1,2}\s+(.+)$", re.MULTILINE)
    boilerplate = {
        "specifications", "features", "utilities", "utility", "ordering information",
        "description", "note", "notes", "sizes", "size", "ansi", "bestseller",
        "warning", "new", "popular in europe",
    }

    boundaries: list[ProductBoundary] = []

    for page_num in sorted(page_map.keys()):
        md = page_map[page_num]

        # Skip cover/index pages
        if _is_cover_or_index_page(md):
            continue

        text = md
        matches = list(heading_re.finditer(text))
        product_starts: list[tuple[int, str]] = []

        for match in matches:
            title = match.group(1).strip()
            if title.lower() in boilerplate:
                continue
            if len(title) < 5:
                continue
            product_starts.append((match.start(), title))

        # Table-orphan pages: attach to last boundary
        has_heading = bool(product_starts)
        if _is_table_orphan(text, has_heading) and boundaries:
            boundaries[-1].page_end = page_num
            if page_num <= len(list(page_map.keys())):
                boundaries[-1].pages_markdown.append(text)
            continue

        if not product_starts:
            continue

        for i, (start, title) in enumerate(product_starts):
            end = product_starts[i + 1][0] if i + 1 < len(product_starts) else len(text)
            section_text = text[start:end].strip()
            code_match = re.search(r"\b([A-Z]{2,6}(?:-[A-Z0-9]+)?)\b", section_text[:400])
            boundaries.append(
                ProductBoundary(
                    page_start=page_num,
                    page_end=page_num,
                    candidate_title=title,
                    candidate_code=code_match.group(1) if code_match else "",
                    confidence=0.3,
                    pages_markdown=[section_text],
                )
            )

    if not boundaries:
        boundaries = [
            ProductBoundary(
                page_start=ocr_results[0].page_num,
                page_end=ocr_results[-1].page_num,
                pages_markdown=[r.markdown for r in ocr_results],
            )
        ]

    logger.info(f"Heuristic boundary detection: {len(boundaries)} product candidates")
    return boundaries


# ── Persistence ───────────────────────────────────────────────────────────────


def save_boundaries(
    boundaries: list[ProductBoundary],
    path: str | Path,
    *,
    pdf_path: str = "",
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pdf_path": pdf_path,
        "boundaries": [b.to_dict() for b in boundaries],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved {len(boundaries)} boundaries → {path}")
    return path


def load_boundaries(
    path: str | Path,
    page_map: dict[int, str],
    *,
    pdf_path: str = "",
) -> list[ProductBoundary] | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            payload = raw
        else:
            cached_pdf = raw.get("pdf_path", "")
            if pdf_path and cached_pdf and cached_pdf != pdf_path:
                logger.warning("Boundaries cache PDF mismatch — re-detecting")
                return None
            payload = raw.get("boundaries", [])
        return [ProductBoundary.from_dict(item, page_map) for item in payload]
    except Exception as exc:
        logger.warning(f"Failed to load boundaries cache: {exc}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def detect_all_boundaries(
    ocr_results: list[PageOCRResult],
    llm_config: LLMConfig | None = None,
    processing_config: ProcessingConfig | None = None,
    *,
    cache_path: str | Path | None = None,
    use_cache: bool = True,
    pdf_path: str = "",
) -> list[ProductBoundary]:
    """
    Detect product family boundaries across all OCR pages.

    Uses Qwen via Groq by default. Pass llm_config=None for heuristic fallback.
    """
    page_map = {r.page_num: r.markdown for r in ocr_results}

    if use_cache and cache_path:
        cached = load_boundaries(cache_path, page_map, pdf_path=pdf_path)
        if cached:
            logger.info(f"Loaded {len(cached)} boundaries from cache")
            return cached

    if llm_config is None:
        boundaries = _detect_boundaries_heuristic(ocr_results)
    else:
        proc = processing_config or ProcessingConfig()
        detector = QwenBoundaryDetector(llm_config, proc)
        boundaries = detector.detect(ocr_results)

    if cache_path:
        save_boundaries(boundaries, cache_path, pdf_path=pdf_path)

    return boundaries
