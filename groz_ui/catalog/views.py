import json
import logging
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services.chat_memory import (
    ADMIN_CHAT_MEMORY_KEY,
    build_memory_update,
    clear_turn,
    load_turn,
    save_turn,
)
from rag_pipeline.ai_router import AIQueryRouterError

from .models import CatalogDocument, DocumentChunk, ProductFamily

PROJECT_ROOT = settings.PROJECT_ROOT
logger = logging.getLogger(__name__)


def _clean_parsing_instructions(value) -> str:
    """Limit optional admin PDF-specific VLM instructions before subprocess use."""
    return str(value or '').strip()[:4000]


def _pipeline_noise_line(line: str) -> bool:
    stripped = str(line or '').strip()
    if not stripped:
        return True
    if re.fullmatch(r'[\^~`\-|. ]+', stripped):
        return True
    if stripped.startswith((
        'Traceback (most recent call last):',
        'During handling of the above exception',
        'The above exception was the direct cause',
        'File "',
    )):
        return True
    if (
        re.search(r'\d+%\|', stripped)
        and '[' in stripped
        and ']' in stripped
    ) or 'it/s]' in stripped:
        return True
    if re.match(r'^(return|raise|await|for|if|elif|else|with|def|class)\b', stripped):
        return True
    return False


def _clean_pipeline_error(output: str) -> str:
    """Extract a useful user-facing error from subprocess stdout/stderr."""
    raw = str(output or '').strip()
    if not raw:
        return 'Pipeline failed without output.'

    lowered = raw.lower()
    if 'high demand' in lowered or 'experiencing high demand' in lowered:
        return 'Google Gemini is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.'
    if '503 unavailable' in lowered or '503 service unavailable' in lowered:
        return 'Google Gemini API is temporarily unavailable (503). Please try again in a few minutes.'
    if 'quota exceeded' in lowered or 'resource_exhausted' in lowered or '429' in lowered:
        return 'API quota limit exceeded. Please wait a moment before trying again.'
    if 'api key not valid' in lowered or 'gemini_api_key is required' in lowered:
        return 'Gemini API Key is missing or invalid. Please configure it in Models & Keys.'
    if '[failed] all retries exhausted' in lowered:
        return 'Gemini extraction failed after all retries for one or more pages.'

    lines = [line.strip() for line in raw.replace('\r', '\n').splitlines() if line.strip()]
    exception_re = re.compile(
        r'^(?:[A-Za-z_][\w.]*\.)?'
        r'(?P<name>[A-Za-z_][\w]*(?:Error|Exception)|InvalidArgument|ResourceExhausted|'
        r'ServiceUnavailable|TooManyRequests|DeadlineExceeded|PermissionDenied|Unauthenticated|'
        r'FailedPrecondition|NotFound|Aborted|RuntimeError|ValueError|SyntaxError):\s*'
        r'(?P<message>.+)$'
    )
    gemini_failed_re = re.compile(r'All Gemini (?:keys|retries) failed:\s*(?P<message>.+)$', re.I)

    for line in reversed(lines):
        if _pipeline_noise_line(line):
            continue
        error_match = exception_re.match(line)
        if error_match:
            return error_match.group('message').strip()[:500]
        if line.upper().startswith('ERROR:'):
            return line.split(':', 1)[1].strip()[:500] or line[:500]
        if line.upper().startswith('WARNING:'):
            warning_message = line.split(':', 1)[1].strip()
            if re.search(r'\b(failed|error)\b', warning_message, re.I):
                return warning_message[:500]
        gemini_match = gemini_failed_re.search(line)
        if gemini_match and gemini_match.group('message').strip().lower() != 'none':
            return gemini_match.group('message').strip()[:500]

    for line in reversed(lines):
        if not _pipeline_noise_line(line):
            return line[:500]

    return 'Pipeline failed. Check the server logs for the full error.'


def _resolve_catalog_document(pdf_name: str):
    from .models import CatalogDocument
    import re

    pdf_name = Path(str(pdf_name or '')).name
    if not pdf_name:
        return None
    pdf_stem = Path(pdf_name).stem

    # Build list of stems to try: exact stem, stem+.pdf, then strip trailing " (N)" suffix
    stems_to_try = [pdf_stem, pdf_name, pdf_stem + '.pdf']
    base_stem = re.sub(r'\s*\(\d+\)$', '', pdf_stem).strip()
    if base_stem and base_stem != pdf_stem:
        stems_to_try.append(base_stem)
        stems_to_try.append(base_stem + '.pdf')

    for s in stems_to_try:
        doc = CatalogDocument.objects.filter(original_filename=s).order_by('-version').first()
        if doc:
            return doc

    # If still not found and this looks like a parent stem, try finding any split part
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
    if not split_re.search(pdf_stem):
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / pdf_stem
        if splits_dir.exists():
            leaf_parts = [p for p in sorted(splits_dir.rglob('*.pdf'))
                          if not (PROJECT_ROOT / 'input' / 'splits' / p.stem).exists()]
            if leaf_parts:
                first_part_stem = leaf_parts[0].stem
                doc = CatalogDocument.objects.filter(original_filename=first_part_stem).order_by('-version').first()
                if doc:
                    return doc

    # Last resort: match by file checksum (handles renamed files like G15.pdf stored as G15-1)
    pdf_with_ext = pdf_name if pdf_name.lower().endswith('.pdf') else pdf_name + '.pdf'
    disk_file = PROJECT_ROOT / 'input' / pdf_with_ext
    if disk_file.exists():
        try:
            import hashlib
            checksum = hashlib.sha256(disk_file.read_bytes()).hexdigest()
            doc = CatalogDocument.objects.filter(checksum_sha256=checksum).order_by('-version').first()
            if doc:
                return doc
        except Exception:
            pass

    return None


def _resolve_split_docs_for_parent(pdf_stem: str):
    """Find all CatalogDocuments for a parent PDF stem that has no direct DB record.

    Handles two layouts:
      1. splits/<pdf_stem>/<parts>.pdf          (standard single-level split)
      2. splits/<pdf_stem>_p*/  sibling dirs    (Hand_Tool style: intermediate splits)
    """
    from .models import CatalogDocument
    import re
    splits_root = PROJECT_ROOT / 'input' / 'splits'
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)

    def _leaf_docs_from_candidates(candidates):
        if not candidates:
            return []
        candidate_names = [Path(str(doc.original_filename or '').strip()).stem for doc in candidates]
        leaf_docs = []
        for doc in candidates:
            name = Path(str(doc.original_filename or '').strip()).stem
            if not name or not split_re.search(name):
                continue
            if any(other != name and other.startswith(name + '_') for other in candidate_names):
                continue
            leaf_docs.append(doc)
        return leaf_docs

    if not splits_root.exists():
        db_candidates = list(CatalogDocument.objects.filter(
            original_filename__startswith=pdf_stem + '_'
        ).order_by('original_filename', '-version', '-id'))
        return _leaf_docs_from_candidates(db_candidates)

    # Layout 1: direct subdir named after the stem
    direct_dir = splits_root / pdf_stem
    if direct_dir.exists():
        leaf_parts = [p for p in sorted(direct_dir.rglob('*.pdf'))
                      if not (splits_root / p.stem).exists()]
        if leaf_parts:
            stems = [p.stem for p in leaf_parts]
            docs = list(CatalogDocument.objects.filter(
                original_filename__in=stems
            ).order_by('original_filename'))
            if docs:
                return docs

    # Layout 2: sibling dirs whose name starts with <pdf_stem>_ (e.g. Hand_Tool_..._p0051-0055)
    sibling_dirs = sorted(
        d for d in splits_root.iterdir()
        if d.is_dir() and d.name.startswith(pdf_stem + '_')
    )
    if sibling_dirs:
        all_leaf_stems = []
        for sib in sibling_dirs:
            leaves = [p for p in sorted(sib.rglob('*.pdf'))
                      if not (splits_root / p.stem).exists()]
            all_leaf_stems.extend(p.stem for p in leaves)
        if all_leaf_stems:
            docs = list(CatalogDocument.objects.filter(
                original_filename__in=all_leaf_stems
            ).order_by('original_filename'))
            if docs:
                return docs

    # DB fallback: resolve leaf split docs even when input/splits/ is absent.
    db_candidates = list(CatalogDocument.objects.filter(
        original_filename__startswith=pdf_stem + '_'
    ).order_by('original_filename', '-version', '-id'))
    db_leaf_docs = _leaf_docs_from_candidates(db_candidates)
    if db_leaf_docs:
        return db_leaf_docs

    return []


def _chunk_excerpt(text: str, limit: int = 180) -> str:
    cleaned = ' '.join(str(text or '').split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit - 1].rstrip() + '…'

_stats_cache: dict = {}
_stats_cache_ts: float = 0.0
_STATS_CACHE_TTL = 8  # seconds


def _invalidate_stats_cache():
    global _stats_cache, _stats_cache_ts
    _stats_cache = {}
    _stats_cache_ts = 0.0


def _get_catalog_stats():
    """Return stats about processed PDFs and indexed chunks."""
    import time
    global _stats_cache, _stats_cache_ts
    if _stats_cache and (time.monotonic() - _stats_cache_ts) < _STATS_CACHE_TTL:
        return _stats_cache
    import re as _re
    input_dir = PROJECT_ROOT / 'input'
    splits_dir = PROJECT_ROOT / 'input' / 'splits'

    pdfs = list(input_dir.glob('*.pdf')) if input_dir.exists() else []

    # Also collect all split parts, keyed by their name for processed lookup
    all_split_parts = []
    split_parent_stems = set()
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                # Collect leaf PDFs at all nesting levels (skip intermediates that have their own subdir)
                parts = [p for p in sorted(stem_dir.rglob('*.pdf')) if not (splits_dir / p.stem).exists()]
                if parts:
                    split_parent_stems.add(stem_dir.name)
                    all_split_parts.extend(parts)

    # For top-level PDFs that have been split, use split parts instead
    effective_pdfs = [p for p in pdfs if p.stem not in split_parent_stems] + all_split_parts
    processed = []

    # DB-only: processed documents with chunks in Postgres
    try:
        from .models import CatalogDocument, DocumentChunk, ProductFamily
        from django.db.models import Count, Q
        # Build filename → disk file map (no checksum reads — fast)
        disk_name_to_file: dict = {p.name: p for p in effective_pdfs}
        disk_stem_to_file: dict = {p.stem: p for p in effective_pdfs}

        # Single query: annotate chunk_count and approved_count per document
        docs = (
            CatalogDocument.objects
            .exclude(status=CatalogDocument.Status.ARCHIVED)
            .annotate(
                chunk_count=Count('chunks', distinct=True),
                approved_count=Count(
                    'product_families',
                    filter=Q(product_families__review_status=ProductFamily.ReviewStatus.APPROVED),
                    distinct=True,
                )
            )
            .order_by('original_filename')
        )
        for doc in docs:
            if doc.chunk_count == 0:
                continue
            fn = doc.original_filename
            fn_pdf = fn if fn.endswith('.pdf') else f'{fn}.pdf'
            disk_file = disk_name_to_file.get(fn_pdf) or disk_name_to_file.get(fn) or disk_stem_to_file.get(fn)
            display = disk_file.name if disk_file else fn_pdf
            processed.append({
                'name': display,
                'chunks': doc.chunk_count,
                'products': doc.approved_count,
                'status': _catalog_document_display_status(doc, chunk_count=doc.chunk_count),
            })
    except Exception:
        pass

    total_chunks = sum(p['chunks'] for p in processed)

    # Dashboard status should reflect PostgreSQL workflow state, not stale vector-store leftovers.
    indexed = 0
    try:
        from .models import DocumentChunk as _DC
        indexed = _DC.objects.filter(index_status=_DC.IndexStatus.INDEXED).count()
    except Exception:
        indexed = 0

    # Build unprocessed list with status info from DB
    processed_names = {proc['name'] for proc in processed}
    processed_stems = {Path(n).stem for n in processed_names}
    unprocessed_raw = [p for p in effective_pdfs
                       if p.name not in processed_names and p.stem not in processed_stems]
    unprocessed = []
    try:
        from .models import CatalogDocument, DocumentChunk, IngestionJob
        for p in unprocessed_raw:
            stem = p.stem
            doc = CatalogDocument.objects.filter(
                original_filename__in=[stem, p.name]
            ).order_by('-version').first()
            status = 'Pending'
            if doc:
                running = IngestionJob.objects.filter(
                    document=doc,
                    status__in=[IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING]
                ).exists()
                has_chunks = DocumentChunk.objects.filter(document=doc).exists()
                if (running or doc.status in (CatalogDocument.Status.EXTRACTING, CatalogDocument.Status.INDEXING)) and not has_chunks:
                    status = 'Processing'
                elif doc.status == CatalogDocument.Status.FAILED:
                    status = 'Failed'
            unprocessed.append({'name': p.name, 'status': status})
    except Exception:
        unprocessed = [{'name': p.name, 'status': 'Pending'} for p in unprocessed_raw]

    try:
        from .models import CatalogDocument, DocumentChunk
        seen_unprocessed = {item['name'] for item in unprocessed}
        for doc in CatalogDocument.objects.exclude(status=CatalogDocument.Status.ARCHIVED).order_by('original_filename'):
            display_name = _display_pdf_name(doc.original_filename)
            if not display_name or display_name in processed_names or display_name in seen_unprocessed:
                continue
            if DocumentChunk.objects.filter(document=doc).exists():
                continue
            unprocessed.append({'name': display_name, 'status': 'Pending'})
            seen_unprocessed.add(display_name)
    except Exception:
        pass

    stats = {
        # Count everything the UI can actually surface: disk PDFs, DB-backed PDFs,
        # and DB-only documents that do not have a matching filesystem file.
        'total_pdfs': len(processed) + len(unprocessed),
        'processed': processed,
        'unprocessed': unprocessed,
        'indexed': indexed,
        'total_chunks': total_chunks,
    }
    try:
        from .models import CatalogDocument, DocumentChunk, IngestionJob, ProductFamily
        stats['v2'] = {
            'documents': CatalogDocument.objects.count(),
            'ready_documents': CatalogDocument.objects.filter(
                status=CatalogDocument.Status.READY,
                is_active=True,
            ).count(),
            'pending_jobs': IngestionJob.objects.filter(
                status__in=(IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING),
            ).count(),
            'families': ProductFamily.objects.count(),
            'needs_review': ProductFamily.objects.filter(
                review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW,
            ).count(),
            'indexed_chunks': indexed,
        }
    except Exception:
        stats['v2'] = {}
    import time
    _stats_cache = stats
    _stats_cache_ts = time.monotonic()
    return stats


# ── Auth ──────────────────────────────────────────────────────────────────────

def admin_login(request):
    error = ''
    if request.method == 'POST':
        user = authenticate(request,
                            username=request.POST.get('username'),
                            password=request.POST.get('password'))
        if user and user.is_active and user.is_staff:
            login(request, user)
            request.session['admin_access_token'] = secrets.token_hex(32)
            request.session.modified = True
            panel = request.GET.get('next_panel', '')
            url = '/admin-panel/' + (f'?panel={panel}' if panel else '')
            return redirect(url)
        elif user and user.is_active and not user.is_staff:
            error = 'Access denied. This login is for administrators only.'
        else:
            error = 'Invalid credentials.'
    else:
        # Clear any existing session when login page is visited via GET
        if request.user.is_authenticated:
            logout(request)
    return render(request, 'catalog/login.html', {'error': error})


def admin_logout(request):
    # Invalidate the token before logout so back-button cached pages fail validation
    request.session.pop('admin_access_token', None)
    # Clear progress tracking so the progress bar starts fresh on next login
    request.session.pop('pdf_progress', None)
    request.session.pop('approved_pdfs', None)
    request.session.modified = True
    # Clear the persisted DB stage map so it doesn't restore on next login
    try:
        from .models import ApiKey
        ApiKey.objects.filter(name='pdf_stage_map').update(value='{}')
    except Exception:
        pass
    logout(request)
    return redirect('/')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    # Reset progress bar state on every page load — user must re-select a PDF
    request.session['pdf_progress'] = {}
    request.session['approved_pdfs'] = []
    request.session.modified = True
    _invalidate_stats_cache()  # force fresh data on next /api/stats/ call after reload
    # Stats are loaded client-side via /api/stats/ on DOMContentLoaded — no server computation needed here
    return render(request, 'catalog/dashboard.html', {
        'is_admin': request.user.is_staff,
        'admin_access_token': request.session.get('admin_access_token', ''),
    })


def _resolve_pdf_path(filename: str) -> Path:
    """Find a PDF file in input/, DB-backed storage, or recursively inside input/splits/ subfolders."""
    filename = Path(filename).name
    input_root = PROJECT_ROOT / 'input'
    pdf_path = input_root / filename
    if pdf_path.exists():
        return pdf_path

    project_pdf_path = PROJECT_ROOT / filename
    if project_pdf_path.exists():
        return project_pdf_path

    # V2 uploads live under MEDIA_ROOT/catalog_documents/..., so prefer the
    # registered CatalogDocument file path before falling back to filesystem scans.
    try:
        doc = _resolve_catalog_document(filename)
        if doc and doc.file and doc.file.name:
            doc_path = _catalog_document_file_path(doc)
            if doc_path:
                return doc_path
    except Exception:
        pass

    splits_dir = input_root / 'splits'
    if splits_dir.exists():
        # Search all levels of nesting under splits/
        for candidate in splits_dir.rglob(filename):
            if candidate.is_file():
                return candidate

    if input_root.exists():
        # Final fallback: search the whole media tree for a matching basename.
        for candidate in input_root.rglob(filename):
            if candidate.is_file():
                return candidate

    return pdf_path


def _catalog_document_file_path(doc):
    if not doc or not getattr(doc, 'file', None):
        return None

    file_name = str(getattr(doc.file, 'name', '') or '').strip()
    if not file_name:
        return None

    candidates = []
    try:
        candidates.append(Path(doc.file.path))
    except Exception:
        pass

    relative = Path(file_name)
    if relative.is_absolute():
        candidates.append(relative)
    else:
        candidates.append(PROJECT_ROOT / relative)
        candidates.append(PROJECT_ROOT / relative.name)

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def _catalog_document_pdf_bytes(doc):
    if not doc:
        return None
    try:
        pdf_binary = getattr(doc, 'pdf_binary', b'') or b''
        if pdf_binary:
            return bytes(pdf_binary)
    except Exception:
        pass

    try:
        doc_path = _catalog_document_file_path(doc)
        if doc_path:
            return doc_path.read_bytes()
    except Exception:
        pass
    return None


_SPLIT_PDF_RE = re.compile(r'^(?P<stem>.+)_(?:custom_)?p(?P<start>\d{4})-(?P<end>\d{4})(?:\.pdf)?$', re.I)


def _split_pdf_match(filename: str):
    name = Path(str(filename or '')).name
    if not name:
        return None
    match = _SPLIT_PDF_RE.match(name)
    if not match:
        return None
    return {
        'name': name,
        'stem': match.group('stem'),
        'start': int(match.group('start')),
        'end': int(match.group('end')),
    }


def _reconstruct_split_pdf_bytes(filename: str, *, _seen=None):
    """Rebuild a split PDF from its parent source when the split file is missing."""
    split_info = _split_pdf_match(filename)
    if not split_info:
        return None

    name = split_info['name']
    if _seen is None:
        _seen = set()
    if name in _seen:
        return None
    _seen.add(name)

    parent_filename = f"{split_info['stem']}.pdf"
    parent_path, parent_bytes, _doc = _resolve_pdf_source_direct(parent_filename)
    if not ((parent_path and parent_path.exists()) or parent_bytes):
        parent_bytes = _reconstruct_split_pdf_bytes(parent_filename, _seen=_seen)
    if not ((parent_path and parent_path.exists()) or parent_bytes):
        return None

    try:
        from io import BytesIO
        from pypdf import PdfReader, PdfWriter

        if parent_path and parent_path.exists():
            reader = PdfReader(str(parent_path))
        else:
            reader = PdfReader(BytesIO(parent_bytes))
        total = len(reader.pages)
        start = split_info['start']
        end = split_info['end']
        if start < 1 or end > total or start > end:
            return None

        writer = PdfWriter()
        for page_idx in range(start - 1, end):
            writer.add_page(reader.pages[page_idx])
        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception:
        return None


def _resolve_pdf_source_direct(filename: str, *, part_file: str = '', stem: str = ''):
    """Return a filesystem path or DB bytes without any split reconstruction."""
    filename = Path(filename or '').name
    part_file = Path(part_file or '').name
    stem = Path(stem or '').name

    if part_file and stem:
        pdf_path = PROJECT_ROOT / 'input' / 'splits' / stem / part_file
        if pdf_path.exists():
            return pdf_path, None, None
        try:
            doc = _resolve_catalog_document(part_file) or _resolve_catalog_document(Path(part_file).stem)
        except Exception:
            doc = None
        pdf_bytes = _catalog_document_pdf_bytes(doc)
        if pdf_bytes:
            return None, pdf_bytes, doc
        return pdf_path, None, doc

    pdf_path = _resolve_pdf_path(filename)
    if pdf_path.exists():
        try:
            doc = _resolve_catalog_document(filename)
        except Exception:
            doc = None
        return pdf_path, None, doc

    try:
        doc = _resolve_catalog_document(filename)
    except Exception:
        doc = None
    pdf_bytes = _catalog_document_pdf_bytes(doc)
    if pdf_bytes:
        return None, pdf_bytes, doc

    return pdf_path, None, doc


def _resolve_pdf_source(filename: str, *, part_file: str = '', stem: str = ''):
    """Return a filesystem path or raw PDF bytes for a requested PDF."""
    pdf_path, pdf_bytes, doc = _resolve_pdf_source_direct(filename, part_file=part_file, stem=stem)
    if pdf_bytes or (pdf_path and pdf_path.exists()):
        return pdf_path, pdf_bytes, doc

    lookup_name = part_file or filename
    reconstructed = _reconstruct_split_pdf_bytes(lookup_name)
    if reconstructed:
        return None, reconstructed, doc

    return pdf_path, None, doc


def _display_pdf_name(original_filename: str) -> str:
    """Return a UI-friendly PDF name with a .pdf suffix when needed."""
    name = Path(str(original_filename or '')).name.strip()
    if not name:
        return ''
    return name if name.lower().endswith('.pdf') else f'{name}.pdf'


def _stage_map_lookup(stage_map: dict, *keys):
    """Look up a stage using the same file-name variants the app writes across sessions."""
    if not isinstance(stage_map, dict):
        return None
    ordered = []
    for raw in keys:
        if raw is None:
            continue
        raw = str(raw).strip()
        if not raw:
            continue
        path = Path(raw)
        variants = [raw, path.name, path.stem]
        if raw.lower().endswith('.pdf'):
            variants.append(path.stem)
        else:
            variants.append(f'{path.stem}.pdf' if path.stem else f'{raw}.pdf')
        for variant in variants:
            variant = str(variant).strip()
            if variant and variant not in ordered:
                ordered.append(variant)
    for key in ordered:
        if key in stage_map:
            return stage_map[key]
    return None


def _stage_map_write(stage_map: dict, filename: str, stage: str):
    """Persist a stage under both the full filename and the stem to avoid stale mismatches."""
    if not isinstance(stage_map, dict):
        return stage_map
    raw = str(filename or '').strip()
    if not raw:
        return stage_map
    path = Path(raw)
    variants = [raw, path.name, path.stem]
    if raw.lower().endswith('.pdf'):
        variants.append(path.stem)
    else:
        variants.append(f'{path.stem}.pdf' if path.stem else f'{raw}.pdf')
    for variant in variants:
        variant = str(variant).strip()
        if variant:
            stage_map[variant] = stage
    return stage_map


def _catalog_document_display_status(doc, *, chunk_count: int = 0, running: bool = False) -> str:
    """Map CatalogDocument state to the UI status labels used across dashboards."""
    if not doc:
        return 'Pending'

    status = str(getattr(doc, 'status', '') or '').lower()
    if status == CatalogDocument.Status.FAILED:
        return 'Failed'
    if running and chunk_count == 0 and status in {
        CatalogDocument.Status.UPLOADED,
        CatalogDocument.Status.EXTRACTING,
        CatalogDocument.Status.INDEXING,
    }:
        return 'Processing'
    if chunk_count > 0:
        return 'Ready'
    if status in {CatalogDocument.Status.EXTRACTING, CatalogDocument.Status.INDEXING}:
        return 'Processing'
    if status in {CatalogDocument.Status.READY, CatalogDocument.Status.REVIEW}:
        return 'Ready'
    return 'Pending'


def _ensure_catalog_document(pdf_path):
    from .models import Catalog, CatalogDocument
    from django.db.models import Max
    import hashlib
    from pypdf import PdfReader

    checksum = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    stem = pdf_path.stem

    # Try finding by checksum first (most reliable)
    doc = CatalogDocument.objects.filter(checksum_sha256=checksum).first()
    if doc:
        return doc

    # Try finding by original_filename
    doc = CatalogDocument.objects.filter(original_filename=stem).order_by('-version').first()
    if not doc:
        doc = CatalogDocument.objects.filter(original_filename=pdf_path.name).order_by('-version').first()
    if doc:
        if not doc.checksum_sha256:
            doc.checksum_sha256 = checksum
            doc.save(update_fields=['checksum_sha256'])
        return doc

    # Use a per-stem catalog slug so each PDF gets its own catalog
    import re as _re
    catalog_slug = _re.sub(r'[^a-z0-9\-]', '-', stem.lower())[:80].strip('-') or 'catalog'
    catalog = Catalog.objects.filter(slug=catalog_slug).first()
    if catalog is None:
        catalog = Catalog.objects.create(name=stem, slug=catalog_slug)

    version = (CatalogDocument.objects.filter(catalog=catalog).aggregate(value=Max('version'))['value'] or 0) + 1

    page_count = 0
    try:
        page_count = len(PdfReader(str(pdf_path)).pages)
    except Exception:
        pass

    doc = CatalogDocument.objects.create(
        catalog=catalog,
        source_type=CatalogDocument.SourceType.CATALOG,
        original_filename=stem,
        checksum_sha256=checksum,
        version=version,
        page_count=page_count,
        status=CatalogDocument.Status.READY,
        is_active=True
    )

    try:
        doc.file.name = str(pdf_path.relative_to(PROJECT_ROOT))
        doc.save(update_fields=['file'])
    except Exception:
        pass

    return doc


def _ingest_assembled_products_for_document(doc, assembled):
    from django.db import transaction
    import re as _re

    # Delete stale Qdrant vectors for this document before replacing chunks
    try:
        from qdrant_client import models as _qmodels
        from rag_pipeline.providers import COLLECTION, build_qdrant_client
        _client = build_qdrant_client()
        if _client.collection_exists(COLLECTION):
            _stem = doc.original_filename
            _sanitized = _re.sub(r'[^a-zA-Z0-9_\-]', '_', _stem)[:60].strip('_')
            for _src in [_stem, _sanitized]:
                try:
                    _client.delete(
                        collection_name=COLLECTION,
                        points_selector=_qmodels.FilterSelector(
                            filter=_qmodels.Filter(
                                must=[_qmodels.FieldCondition(
                                    key='metadata.source_pdf',
                                    match=_qmodels.MatchValue(value=_src),
                                )]
                            )
                        ),
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Temporarily set is_active = False so we can update / replace the products
    was_active = doc.is_active
    if was_active:
        doc.is_active = False
        doc.save(update_fields=['is_active'])
        
    try:
        # Use transaction to ensure consistency
        with transaction.atomic():
            from .services.structured_ingestion import persist_assembled_products
            from .models import DocumentChunk, ProductFamily
            # Explicitly delete ALL existing chunks and families for clean re-creation
            DocumentChunk.objects.filter(document=doc).delete()
            ProductFamily.objects.filter(document=doc).delete()
            persist_assembled_products(doc, assembled, replace=False)
    finally:
        # Restore is_active state
        doc.is_active = True
        doc.status = doc.Status.READY
        doc.save(update_fields=['is_active', 'status'])
        _invalidate_stats_cache()


# ── API: Run Mistral OCR Pipeline (pdf_extractor) ────────────────────────────

@login_required
@require_POST
def run_pipeline_mistral(request):
    """Run Mistral OCR-4 + Groq LLM pipeline on a whole PDF."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        filename = body.get('filename', '')
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = _resolve_pdf_path(filename)
    if not pdf_path.exists():
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.mistral_api_key:
            return JsonResponse({'error': 'Mistral API key is not configured. Open Models & Keys.'}, status=400)
        if not runtime.groq_api_key:
            return JsonResponse({'error': 'Groq API key is required for Mistral OCR-4 + LLM extraction. Open Models & Keys.'}, status=400)

        env = subprocess_environment()
        runner = str(PROJECT_ROOT / 'pdf_extractor' / 'run_mistral_pipeline.py')
        result = subprocess.run(
            [sys.executable, runner, '--pdf', str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT / 'pdf_extractor'),
            timeout=600,
            env=env,
        )
        stderr_out = result.stderr or ''
        stdout_out = result.stdout or ''
        logger.info('[mistral-pipeline] pdf=%s rc=%d\nSTDERR:\n%s', filename, result.returncode, stderr_out)

        if result.returncode != 0:
            error_text = stderr_out + stdout_out
            return JsonResponse({
                'error': _clean_pipeline_error(error_text) or 'Mistral pipeline failed.',
                'detail': error_text[-4000:],
            }, status=500)

        # Parse structured JSON from stdout
        try:
            data = json.loads(stdout_out.strip())
        except Exception:
            return JsonResponse({
                'error': 'Mistral pipeline returned invalid output.',
                'detail': stdout_out[-2000:],
            }, status=500)

        assembled = _build_assembled_from_mistral(data, pdf_path)
        doc = _ensure_catalog_document(pdf_path)
        _ingest_assembled_products_for_document(doc, assembled)

        pdf_progress = request.session.get('pdf_progress', {})
        approved_pdfs = request.session.get('approved_pdfs', [])
        if filename in approved_pdfs:
            pdf_progress[filename] = 'chunked'
            request.session['pdf_progress'] = pdf_progress
            request.session.modified = True

        return JsonResponse({'message': 'Mistral OCR pipeline completed.', 'output': stderr_out[-3000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out (10 min limit).'}, status=500)
    except Exception as e:
        logger.exception('[mistral-pipeline] unexpected error')
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def run_pipeline_mistral_split(request):
    """Run Mistral OCR-4 + Groq LLM pipeline on a single split part."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        stem = body.get('stem', '').strip()
        part_file = body.get('part_file', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = PROJECT_ROOT / 'input' / 'splits' / stem / part_file
    if not pdf_path.exists():
        return JsonResponse({'error': f'Split file not found: {part_file}'}, status=404)

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.mistral_api_key:
            return JsonResponse({'error': 'Mistral API key is not configured. Open Models & Keys.'}, status=400)
        if not runtime.groq_api_key:
            return JsonResponse({'error': 'Groq API key is required for Mistral OCR-4 + LLM extraction.'}, status=400)

        env = subprocess_environment()
        runner = str(PROJECT_ROOT / 'pdf_extractor' / 'run_mistral_pipeline.py')
        result = subprocess.run(
            [sys.executable, runner, '--pdf', str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT / 'pdf_extractor'),
            timeout=600,
            env=env,
        )
        stderr_out = result.stderr or ''
        stdout_out = result.stdout or ''
        logger.info('[mistral-split] part=%s rc=%d\nSTDERR:\n%s', part_file, result.returncode, stderr_out)

        if result.returncode != 0:
            error_text = stderr_out + stdout_out
            return JsonResponse({
                'error': _clean_pipeline_error(error_text) or 'Mistral pipeline failed.',
                'detail': error_text[-4000:],
            }, status=500)

        try:
            data = json.loads(stdout_out.strip())
        except Exception:
            return JsonResponse({
                'error': 'Mistral pipeline returned invalid output.',
                'detail': stdout_out[-2000:],
            }, status=500)

        assembled = _build_assembled_from_mistral(data, pdf_path)
        doc = _ensure_catalog_document(pdf_path)
        _ingest_assembled_products_for_document(doc, assembled)

        import json as _j
        from .models import ApiKey
        try:
            _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            _stage_map = {}
        _stage_map[part_file] = 'chunked'
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
        all_parts = sorted(p.name for p in splits_dir.glob('*.pdf')) if splits_dir.exists() else [part_file]
        done_parts = [p for p in all_parts if _stage_map.get(p) in ('chunked', 'families', 'indexed', 'tested')]
        if len(done_parts) == len(all_parts):
            _stage_map[stem] = 'chunked'
        ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})

        pdf_progress = request.session.get('pdf_progress', {})
        pdf_progress[part_file] = 'chunked'
        if len(done_parts) == len(all_parts):
            pdf_progress[stem] = 'chunked'
        request.session['pdf_progress'] = pdf_progress
        request.session.modified = True

        return JsonResponse({'message': f'{part_file} processed via Mistral OCR.', 'output': stderr_out[-2000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out.'}, status=500)
    except Exception as e:
        logger.exception('[mistral-split] unexpected error')
        return JsonResponse({'error': str(e)}, status=500)


def _build_assembled_from_mistral(data: dict, pdf_path: Path) -> list:
    """Convert run_mistral_pipeline.py output into assembled product list for ingestion."""
    products = data.get('products', [])
    markdown_text = data.get('markdown', '')
    source_pdf = data.get('source_pdf', pdf_path.name)

    if not products:
        # Fallback: ingest the whole markdown as one product
        return [{
            'product_name': pdf_path.stem.replace('_', ' ').title(),
            'product_code': '',
            'category': '',
            'description': markdown_text[:500],
            'features': [],
            'utilities': [],
            'specifications': {},
            'children': [],
            'page_start': 1,
            'page_end': 1,
            'source_pdf': source_pdf,
            'raw_text': markdown_text,
        }]

    assembled = []
    for p in products:
        if not isinstance(p, dict):
            continue
        name = str(p.get('product_name') or '').strip()
        if not name:
            continue
        # Prefer per-product raw_text; fall back to full markdown only if needed
        product_raw = str(p.get('raw_text') or p.get('_chunk_text') or markdown_text)
        chunk_text = p.get('_chunk_text')
        assembled.append({
            'product_name': name,
            'product_code': str(p.get('product_code') or ''),
            'category': str(p.get('category') or ''),
            'description': str(p.get('description') or ''),
            'features': p.get('features') if isinstance(p.get('features'), list) else [],
            'utilities': p.get('utilities') if isinstance(p.get('utilities'), list) else [],
            'specifications': p.get('specifications') if isinstance(p.get('specifications'), dict) else {},
            'children': p.get('children') if isinstance(p.get('children'), list) else [],
            'page_start': int(p.get('page_start') or 1),
            'page_end': int(p.get('page_end') or 1),
            'source_pdf': source_pdf,
            'raw_text': product_raw,
            **({'_chunk_text': chunk_text} if chunk_text not in (None, '') else {}),
        })
    return assembled or [{
        'product_name': pdf_path.stem.replace('_', ' ').title(),
        'product_code': '',
        'category': '',
        'description': markdown_text[:500],
        'features': [], 'utilities': [], 'specifications': {}, 'children': [],
        'page_start': 1, 'page_end': 1,
        'source_pdf': source_pdf, 'raw_text': markdown_text,
    }]


def _ingest_mistral_output_for_pdf(pdf_path: Path, env: dict) -> None:
    pass  # replaced by direct ingestion in the views above


# ── API: Run Vision Pipeline ──────────────────────────────────────────────────

@login_required
@require_POST
def run_pipeline(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body     = json.loads(request.body)
        filename = body.get('filename', '')
        parsing_instructions = _clean_parsing_instructions(body.get('parsing_instructions'))
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = _resolve_pdf_path(filename)
    if not pdf_path.exists():
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.gemini_api_key:
            return JsonResponse({'error': 'Gemini API key is not configured. Open Models & Keys.'}, status=400)
        env = subprocess_environment()
        if parsing_instructions:
            env['PDF_PARSING_INSTRUCTIONS'] = parsing_instructions
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=600,
            env=env,
        )
        output = result.stdout or ''
        logger.info('[pipeline] pdf=%s rc=%d\n%s', filename, result.returncode, output)
        if result.returncode == 1:
            return JsonResponse({
                'error': _clean_pipeline_error(output),
                'detail': output[-4000:],
                'output': output[-4000:],
            }, status=500)
        if result.returncode == 2:
            return JsonResponse({
                'error': _clean_pipeline_error(output),
                'detail': output[-4000:],
                'output': output[-4000:],
            }, status=500)
        if result.returncode == 3:
            return JsonResponse({
                'error': 'No products could be extracted from this PDF. All pages failed validation.',
                'detail': output,
                'output': output,
            }, status=422)

        # Update progress for this specific PDF to 'chunked' (50%)
        pdf_progress = request.session.get('pdf_progress', {})
        approved_pdfs = request.session.get('approved_pdfs', [])
        if filename in approved_pdfs:
            pdf_progress[filename] = 'chunked'
            request.session['pdf_progress'] = pdf_progress
            request.session.modified = True

        # Pipeline ingests directly into Postgres — no post-run file reading needed
        return JsonResponse({'message': 'Pipeline completed.', 'output': output[-3000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out (10 min limit).'}, status=500)
    except Exception as e:
        logger.exception('[pipeline] unexpected error pdf=%s', filename)
        return JsonResponse({'error': str(e)}, status=500)


# ── API: List uploaded PDFs ──────────────────────────────────────────────────

@login_required
def list_pdfs(request):
    input_dir = PROJECT_ROOT / 'input'
    splits_dir = PROJECT_ROOT / 'input' / 'splits'
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
    
    pdfs = sorted(p.name for p in input_dir.glob('*.pdf')) if input_dir.exists() else []
    
    # Track which parent PDFs have been split
    split_parent_stems = set()
    
    # Add split PDFs from input/splits/ (all nesting levels)
    split_pdfs = []
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if not stem_dir.is_dir():
                continue
            # Collect all PDFs recursively under this stem dir
            all_files = sorted(stem_dir.rglob('*.pdf'))
            if all_files:
                split_parent_stems.add(stem_dir.name)
                for split_pdf in all_files:
                    # Only add leaf PDFs — skip intermediates that have their own splits subdir
                    if not (splits_dir / split_pdf.stem).exists():
                        split_pdfs.append(split_pdf.name)
    
    # Filter out parent PDFs that have been split
    filtered_pdfs = []
    for pdf in pdfs:
        pdf_stem = Path(pdf).stem
        # Only include if this PDF has NOT been split
        if pdf_stem not in split_parent_stems:
            filtered_pdfs.append(pdf)
    
    # Combine filtered main PDFs and split PDFs
    all_pdfs = filtered_pdfs + split_pdfs

    # Gather rich metadata for each PDF/part
    pdf_details = []
    try:
        from django.db.models import Count, Q
        from .models import CatalogDocument, DocumentChunk, IngestionJob, ProductFamily

        docs = list(
            CatalogDocument.objects.exclude(status=CatalogDocument.Status.ARCHIVED)
            .order_by('original_filename', '-version', '-id')
            .only('id', 'original_filename', 'checksum_sha256', 'status', 'version')
        )

        docs_by_name = {}
        docs_by_checksum = {}
        for doc in docs:
            original_name = Path(str(doc.original_filename or '').strip()).name
            display_name = _display_pdf_name(original_name)
            stem = Path(original_name).stem
            for key in (original_name, display_name, stem):
                if key and key not in docs_by_name:
                    docs_by_name[key] = doc
            if doc.checksum_sha256 and doc.checksum_sha256 not in docs_by_checksum:
                docs_by_checksum[doc.checksum_sha256] = doc

        doc_ids = [doc.id for doc in docs]
        chunk_stats = {}
        if doc_ids:
            for row in (
                DocumentChunk.objects.filter(document_id__in=doc_ids)
                .values('document_id')
                .annotate(
                    total=Count('id'),
                    indexed=Count('id', filter=Q(index_status=DocumentChunk.IndexStatus.INDEXED)),
                    stale=Count('id', filter=Q(index_status=DocumentChunk.IndexStatus.STALE)),
                )
            ):
                chunk_stats[row['document_id']] = row

        family_counts = {}
        family_review_counts = {}
        if doc_ids:
            for row in (
                ProductFamily.objects.filter(
                    document_id__in=doc_ids,
                )
                .values('document_id')
                .annotate(
                    approved=Count('id', filter=Q(review_status=ProductFamily.ReviewStatus.APPROVED)),
                    needs_review=Count('id', filter=Q(review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW)),
                    rejected=Count('id', filter=Q(review_status=ProductFamily.ReviewStatus.REJECTED)),
                )
            ):
                family_counts[row['document_id']] = row.get('approved', 0) or 0
                family_review_counts[row['document_id']] = row

        running_job_ids = set(
            IngestionJob.objects.filter(
                document_id__in=doc_ids,
                status__in=[IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING],
            ).values_list('document_id', flat=True)
        )

        # Add DB-backed documents even when there is no matching file in input/.
        # This keeps the UI aligned with the actual CatalogDocument table instead of
        # relying only on what happens to exist on disk.
        seen_pdfs = set(all_pdfs)
        for doc in docs:
            display_name = _display_pdf_name(doc.original_filename)
            if display_name and display_name not in seen_pdfs:
                all_pdfs.append(display_name)
                seen_pdfs.add(display_name)

        # Also include intermediate split parts that have their own chunks in DB,
        # even if their sub-parts also have chunks (both sets of chunks are valid).
        disk_pdf_stems = {Path(p).stem for p in all_pdfs}
        for doc in docs:
            original_name = Path(str(doc.original_filename or '').strip()).name
            if not original_name:
                continue
            stem = Path(original_name).stem
            if stem in disk_pdf_stems:
                continue
            if not split_re.search(stem):
                continue
            if chunk_stats.get(doc.id, {}).get('total', 0) == 0:
                continue
            display_name = _display_pdf_name(original_name)
            if display_name and display_name not in seen_pdfs:
                all_pdfs.append(display_name)
                seen_pdfs.add(display_name)
                disk_pdf_stems.add(stem)

        # Build set of stems that already have chunks (DB only)
        chunked = {
            doc.original_filename
            for doc in docs
            if chunk_stats.get(doc.id, {}).get('total', 0) > 0
        }

        for pdf_name in all_pdfs:
            pdf_name = Path(pdf_name).name
            stem = Path(pdf_name).stem
            doc = docs_by_name.get(pdf_name) or docs_by_name.get(stem)

            # Last resort: if a disk filename was renamed after upload, try a checksum
            # match only for this one item instead of querying the database repeatedly.
            if not doc:
                pdf_path = input_dir / pdf_name
                if pdf_path.exists():
                    try:
                        import hashlib
                        checksum = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                        doc = docs_by_checksum.get(checksum)
                    except Exception:
                        pass

            chunks_count = 0
            product_families_count = 0
            approved_families_count = 0
            needs_review_families_count = 0
            rejected_families_count = 0
            review_status = 'pending'
            status = 'Pending'
            doc_id = None
            has_embeddings = False

            # For split parts with no own DB doc, fall back to parent doc
            if not doc and split_re.search(Path(pdf_name).stem):
                parent_stem = split_re.sub('', Path(pdf_name).stem)
                parent_doc = docs_by_name.get(parent_stem + '.pdf') or docs_by_name.get(parent_stem)
                if parent_doc:
                    doc = parent_doc

            if doc:
                doc_id = str(doc.id)
                stats = chunk_stats.get(doc.id, {})
                chunks_count = stats.get('total', 0) or 0
                review_stats = family_review_counts.get(doc.id, {})
                approved_families_count = family_counts.get(doc.id, 0) or 0
                needs_review_families_count = review_stats.get('needs_review', 0) or 0
                rejected_families_count = review_stats.get('rejected', 0) or 0
                if needs_review_families_count > 0:
                    review_status = ProductFamily.ReviewStatus.NEEDS_REVIEW
                elif approved_families_count > 0:
                    review_status = ProductFamily.ReviewStatus.APPROVED
                elif chunks_count > 0:
                    review_status = ProductFamily.ReviewStatus.NEEDS_REVIEW
                else:
                    review_status = 'pending'
                product_families_count = approved_families_count

                status = _catalog_document_display_status(
                    doc,
                    chunk_count=chunks_count,
                    running=doc.id in running_job_ids,
                )

                has_indexed = (stats.get('indexed', 0) or 0) > 0
                has_stale = (stats.get('stale', 0) or 0) > 0
                has_embeddings = has_indexed and not has_stale

            pdf_details.append({
                'name': pdf_name,
                'chunks_count': chunks_count,
                'product_families_count': product_families_count,
                'approved_families_count': approved_families_count,
                'needs_review_families_count': needs_review_families_count,
                'rejected_families_count': rejected_families_count,
                'review_status': review_status,
                'status': status,
                'document_id': doc_id,
                'has_embeddings': has_embeddings
            })
    except Exception as exc:
        import logging
        logging.error(f"Error gathering pdf_details: {exc}", exc_info=True)

    return JsonResponse({
        'pdfs': all_pdfs, 
        'chunked': list(chunked),
        'pdf_details': pdf_details,
    })


# ── API: Approve PDF after preview ───────────────────────────────────────────

@login_required
@require_POST
def approve_pdf(request):
    """Mark a PDF as approved after preview - detect and set its actual progress stage."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        filename = body.get('filename', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    
    if not filename:
        return JsonResponse({'error': 'Missing filename.'}, status=400)

    # Auto-detect the actual stage based on what's been completed
    from .models import CatalogDocument, DocumentChunk, ApiKey
    from pathlib import Path
    import json as _j

    STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested']
    detected_stage = 'uploaded'
    pdf_stem = Path(filename).stem
    done_count = None

    # If caller passed split_parts list (parent stem preview), aggregate from all parts
    split_parts = body.get('split_parts', []) if isinstance(body, dict) else []

    if split_parts:
        try:
            _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            _stage_map = {}

        part_stages = []
        for part_name in split_parts:
            part_stem = Path(part_name).stem
            # Check DB stage map for this part OR its parent stem
            _split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
            parent_stem = _split_re.sub('', part_stem)
            part_stage = (_stage_map.get(part_name) or _stage_map.get(part_stem)
                          or _stage_map.get(parent_stem) or _stage_map.get(parent_stem + '.pdf'))
            if part_stage in STAGE_ORDER:
                part_stages.append(part_stage)
            else:
                # Check DB chunks — fall back to parent doc if no own doc
                try:
                    doc = CatalogDocument.objects.filter(
                        original_filename__in=[part_stem, part_name]
                    ).order_by('-version').first()
                    if not doc:
                        doc = CatalogDocument.objects.filter(
                            original_filename__in=[parent_stem, parent_stem + '.pdf']
                        ).order_by('-version').first()
                    if doc:
                        indexed_count = DocumentChunk.objects.filter(
                            document=doc, index_status=DocumentChunk.IndexStatus.INDEXED
                        ).count()
                        chunks_count = DocumentChunk.objects.filter(document=doc).count()
                        families_count = ProductFamily.objects.filter(document=doc).count()
                        if indexed_count > 0:
                            part_stages.append('indexed')
                        elif families_count > 0:
                            part_stages.append('families')
                        elif chunks_count > 0:
                            part_stages.append('chunked')
                        else:
                            part_stages.append('uploaded')
                    else:
                        part_stages.append('uploaded')
                except Exception:
                    part_stages.append('uploaded')

        if part_stages:
            done_count = sum(1 for s in part_stages if s != 'uploaded')
            # Overall progress = minimum stage (all parts must reach a stage)
            detected_stage = min(part_stages, key=lambda s: STAGE_ORDER.index(s))
    else:
        try:
            doc = _resolve_catalog_document(filename)

            if doc:
                chunks_count = DocumentChunk.objects.filter(document=doc).count()
                indexed_count = DocumentChunk.objects.filter(
                    document=doc,
                    index_status=DocumentChunk.IndexStatus.INDEXED
                ).count()
                families_count = ProductFamily.objects.filter(document=doc, review_status=ProductFamily.ReviewStatus.APPROVED).count()
                if indexed_count > 0:
                    detected_stage = 'indexed'
                elif families_count > 0:
                    detected_stage = 'families'
                elif chunks_count > 0:
                    detected_stage = 'chunked'
        except Exception:
            pass

    # Load persisted stage from DB (survives logout/login)
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
    except Exception:
        _stage_map = {}
    # Also check by resolved doc's original_filename (handles renamed files like G3P-CL -> G3P)
    _resolved_doc = _resolve_catalog_document(filename)
    _resolved_stem = _resolved_doc.original_filename if _resolved_doc else None
    db_stage = _stage_map_lookup(_stage_map, filename, pdf_stem, _resolved_stem)
    if db_stage not in STAGE_ORDER:
        db_stage = None

    # Preserve higher persisted/session stages even when a fresh detection misses
    # chunk/index records (for example after a reload or stale preview data). The
    # final tested stage must never be downgraded by a lower fallback value.
    existing_session_stage = request.session.get('pdf_progress', {}).get(filename) or request.session.get('pdf_progress', {}).get(pdf_stem)
    candidate_stages = [s for s in [detected_stage, existing_session_stage, db_stage] if s in STAGE_ORDER]
    if not candidate_stages:
        final_stage = 'uploaded'
    else:
        final_stage = max(candidate_stages, key=lambda s: STAGE_ORDER.index(s))

    # Replace — only track one PDF at a time, while keeping a stem alias for compatibility
    request.session['approved_pdfs'] = [filename]
    pdf_progress = request.session.get('pdf_progress', {})
    pdf_progress[filename] = final_stage
    if pdf_stem:
        pdf_progress[pdf_stem] = final_stage
    request.session['pdf_progress'] = pdf_progress
    request.session.modified = True

    return JsonResponse({
        'message': f'"{filename}" approved and ready for processing.',
        'stage': final_stage,
        'done_count': done_count if split_parts else None,
        'total_count': len(split_parts) if split_parts else None,
    })


@login_required
@require_POST
def update_pdf_stage(request):
    """Update a PDF's progress stage: uploaded, chunked, families, indexed, tested."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        filename = body.get('filename', '').strip()
        stage = body.get('stage', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    
    if not filename or stage not in ['uploaded', 'chunked', 'families', 'indexed', 'tested']:
        return JsonResponse({'error': 'Invalid filename or stage.'}, status=400)

    STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested']

    # Persist to DB (ApiKey table) so stage survives logout/login
    from .models import ApiKey
    import json as _j
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
    except Exception:
        _stage_map = {}
    current_db_stage = _stage_map_lookup(_stage_map, filename, Path(filename).stem)
    if current_db_stage not in STAGE_ORDER:
        current_db_stage = 'uploaded'

    # Never downgrade — EXCEPT: allow indexed to overwrite tested (re-embedding after delete)
    if STAGE_ORDER.index(stage) < STAGE_ORDER.index(current_db_stage):
        if not (stage == 'indexed' and current_db_stage == 'tested'):
            stage = current_db_stage

    _stage_map_write(_stage_map, filename, stage)
    ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})

    pdf_progress = request.session.get('pdf_progress', {})
    pdf_progress[filename] = stage
    pdf_progress[Path(filename).stem] = stage
    request.session['pdf_progress'] = pdf_progress
    approved_pdfs = request.session.get('approved_pdfs', [])
    canonical = Path(filename).stem or filename
    if canonical not in approved_pdfs:
        approved_pdfs = [canonical]
    request.session['approved_pdfs'] = approved_pdfs
    request.session.modified = True

    return JsonResponse({'message': f'"{filename}" stage updated to {stage}.', 'stage': stage})


# ── API: PDF Preview (page thumbnails) ───────────────────────────────────

@login_required
def pdf_preview(request):
    import base64, fitz
    filename = request.GET.get('pdf', '').strip()
    part_file = request.GET.get('part', '').strip()
    stem = request.GET.get('stem', '').strip()
    if part_file and not stem:
        return JsonResponse({'error': 'Missing split stem.'}, status=400)
    if not filename and not part_file:
        return JsonResponse({'error': 'No PDF specified.'}, status=400)

    pdf_path, pdf_bytes, _doc = _resolve_pdf_source(
        filename or part_file,
        part_file=part_file,
        stem=stem,
    )

    has_path = bool(pdf_path and pdf_path.exists())
    has_bytes = bool(pdf_bytes)
    if not has_path and not has_bytes:
        return JsonResponse({'error': 'PDF not found.'}, status=404)
    try:
        if has_path:
            doc = fitz.open(str(pdf_path))
        else:
            doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        total = len(doc)
        # Support paginated loading: ?page=1&per_page=50
        per_page = min(200, max(1, int(request.GET.get('per_page', total))))
        page_num = max(1, int(request.GET.get('page', 1)))
        start = (page_num - 1) * per_page
        end   = min(start + per_page, total)
        pages = []
        for i in range(start, end):
            thumb = doc[i].get_pixmap(matrix=fitz.Matrix(0.2, 0.2))
            full  = doc[i].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            thumb_b64 = base64.b64encode(thumb.pil_tobytes(format='JPEG', optimize=True, quality=60)).decode()
            full_b64  = base64.b64encode(full.pil_tobytes(format='JPEG', optimize=True, quality=85)).decode()
            pages.append({
                'num':   i + 1,
                'thumb': f'data:image/jpeg;base64,{thumb_b64}',
                'full':  f'data:image/jpeg;base64,{full_b64}',
            })
        doc.close()
        return JsonResponse({
            'pages':    pages,
            'total':    total,
            'page':     page_num,
            'per_page': per_page,
            'has_more': end < total,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Admin Chat ───────────────────────────────────────────────────────────

@login_required
@require_POST
def admin_chat(request):
    try:
        body  = json.loads(request.body)
        query = body.get('query', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    if not query:
        return JsonResponse({'error': 'Empty query.'}, status=400)

    try:
        from .model_config import get_runtime_config
        from .services.query_engine import CatalogQueryEngine

        memory_turn = load_turn(request.session, ADMIN_CHAT_MEMORY_KEY)
        runtime = get_runtime_config()
        if not runtime.embedding_api_key:
            provider_label = 'Gemini' if runtime.embedding_provider == 'gemini' else 'OpenAI'
            return JsonResponse(
                {'error': f'{provider_label} API key is required for retrieval embeddings.'},
                status=400,
            )
        if not runtime.chat_api_key:
            return JsonResponse({'error': f'{runtime.chat_provider.title()} API key is not configured.'}, status=400)
        catalog_ids = body.get('catalog_ids', [])
        document_ids = body.get('document_ids', [])
        if not isinstance(catalog_ids, list) or not isinstance(document_ids, list):
            return JsonResponse({'error': 'catalog_ids and document_ids must be arrays.'}, status=400)
        execution = CatalogQueryEngine(runtime).execute(
            query,
            catalog_ids=catalog_ids,
            document_ids=document_ids,
            page=max(1, int(body.get('page', 1))),
            page_size=min(100, max(1, int(body.get('page_size', 50)))),
            memory=memory_turn.model_dump(mode='json', exclude_defaults=True, exclude_none=True) or None,
        )
        payload = execution.as_dict()
        payload['query_path'] = 'v2'
        try:
            memory_update = build_memory_update(
                query,
                execution,
                standalone_query=getattr(execution, 'standalone_query', None),
            )
            if memory_update is None:
                clear_turn(request.session, ADMIN_CHAT_MEMORY_KEY)
            else:
                save_turn(request.session, ADMIN_CHAT_MEMORY_KEY, memory_update)
        except Exception:
            logger.exception('Failed to persist admin chat memory.')
        return JsonResponse(payload)
    except AIQueryRouterError:
        correlation_id = str(uuid.uuid4())
        logger.exception('AI query router failed; correlation_id=%s', correlation_id)
        return JsonResponse({
            'error': 'AI query router failed.',
            'correlation_id': correlation_id,
        }, status=502)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Catalog Stats (for dashboard refresh) ────────────────────────────────

def admin_ping(request):
    if not request.user.is_authenticated or not request.user.is_staff or not request.session.get('admin_access_token'):
        return JsonResponse({'ok': False}, status=401)
    return JsonResponse({'ok': True})


@login_required
def catalog_stats(request):
    if not request.user.is_staff or not request.session.get('admin_access_token'):
        return JsonResponse({'error': 'Session expired.'}, status=403)
    stats = _get_catalog_stats()

    pdf_progress = request.session.get('pdf_progress', {})
    approved_pdfs = request.session.get('approved_pdfs', [])

    # Only restore from DB if the session already has an active tracked PDF
    # (i.e. user has interacted this session). On fresh page load, keep it empty.
    from .models import ApiKey
    STAGE_ORDER = ['uploaded', 'chunked', 'families', 'indexed', 'tested']
    db_map = {}
    if approved_pdfs:  # only restore when user has already selected something
        try:
            import json as _j
            db_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            db_map = {}
        for fname, db_stage in db_map.items():
            if db_stage not in STAGE_ORDER:
                continue
            session_stage = pdf_progress.get(fname)
            if not session_stage or STAGE_ORDER.index(db_stage) > STAGE_ORDER.index(session_stage):
                pdf_progress[fname] = db_stage
                if fname not in approved_pdfs:
                    approved_pdfs = [fname]
    else:
        try:
            import json as _j
            db_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            db_map = {}
        # Fresh page load — do NOT restore tracked PDF. Progress bar stays blank
        # until user explicitly clicks a PDF row.

    request.session['pdf_progress'] = pdf_progress
    request.session['approved_pdfs'] = approved_pdfs
    request.session.modified = True

    stats['pdf_progress'] = pdf_progress
    stats['approved_pdfs'] = approved_pdfs

    # Compute tracked_pdf, tracked_stage, tracked_percent for the progress bar
    # Prefer parent stem (without split suffix) as tracked_pdf
    import re as _re
    splits_dir = PROJECT_ROOT / 'input' / 'splits'
    split_re = _re.compile(r'_(custom_)?p\d{4}-\d{4}(\.pdf)?$', _re.I)
    tracked_pdf = None
    tracked_stage = None
    # Find the most recently approved entry, preferring parent stems
    for fname in reversed(approved_pdfs):
        if split_re.search(fname):
            # This is a split part — use parent stem instead
            parent = split_re.sub('', fname).rstrip('.')
            if not tracked_pdf:
                tracked_pdf = parent
                tracked_stage = 'uploaded'  # will be recomputed below from actual data
        else:
            # Check if this is a parent stem that has a splits directory
            split_parts_dir_check = splits_dir / fname
            if split_parts_dir_check.exists() and list(split_parts_dir_check.glob('*.pdf')):
                # Has splits — don't trust session stage, recompute from actual data
                tracked_pdf = fname
                tracked_stage = 'uploaded'  # will be recomputed below
            else:
                tracked_pdf = fname
                # Check both the full filename and the stem against stage map
                _stem = Path(fname).stem
                _stage_from_progress = pdf_progress.get(fname) or pdf_progress.get(_stem)
                _stage_from_db = _stage_map_lookup(db_map, fname, _stem)
                # Pick highest stage between session and DB
                _candidates = [s for s in [_stage_from_progress, _stage_from_db] if s in STAGE_ORDER]
                tracked_stage = max(_candidates, key=lambda s: STAGE_ORDER.index(s)) if _candidates else 'uploaded'
                # Upgrade chunked → families only if user has approved at least one family
                if tracked_stage == 'chunked':
                    try:
                        from .models import CatalogDocument
                        _doc = CatalogDocument.objects.filter(
                            original_filename__in=[fname, _stem]
                        ).order_by('-version').first()
                        if _doc and ProductFamily.objects.filter(document=_doc, review_status=ProductFamily.ReviewStatus.APPROVED).exists():
                            tracked_stage = 'families'
                    except Exception:
                        pass
            break
    if not tracked_stage or tracked_stage not in STAGE_ORDER:
        tracked_stage = 'uploaded'
    tracked_percent = None
    total_parts = None

    if tracked_pdf and tracked_stage in STAGE_ORDER:
        # Check if this is a parent stem with split parts — always recompute from actual data
        split_parts_dir = splits_dir / tracked_pdf
        if split_parts_dir.exists():
            split_part_files = sorted([f.name for f in split_parts_dir.glob('*.pdf')])
            if split_part_files:
                try:
                    from .models import CatalogDocument, DocumentChunk
                    import re as _re2
                    total_parts = len(split_part_files)
                    chunked_count = 0
                    indexed_count = 0

                    for p in split_part_files:
                        part_stem = Path(p).stem
                        has_chunks = False
                        has_index  = False

                        # --- Check chunks: session/DB stage map ---
                        if (_stage_map_lookup(pdf_progress, p, part_stem) or _stage_map_lookup(db_map, p, part_stem)) in STAGE_ORDER[1:]:
                            has_chunks = True
                        # DB DocumentChunk records
                        if not has_chunks:
                            try:
                                doc = CatalogDocument.objects.filter(
                                    original_filename__in=[part_stem, p]
                                ).first()
                                if doc and DocumentChunk.objects.filter(document=doc).exists():
                                    has_chunks = True
                            except Exception:
                                pass

                        if has_chunks:
                            chunked_count += 1

                        # --- Check indexed: session/DB stage map ---
                        part_stage = _stage_map_lookup(pdf_progress, p, part_stem) or _stage_map_lookup(db_map, p, part_stem)
                        if part_stage in ('indexed', 'tested'):
                            has_index = True
                        # DB indexed chunks
                        if not has_index:
                            try:
                                doc = CatalogDocument.objects.filter(
                                    original_filename__in=[part_stem, p]
                                ).first()
                                if doc and DocumentChunk.objects.filter(
                                    document=doc,
                                    index_status=DocumentChunk.IndexStatus.INDEXED
                                ).exists():
                                    has_index = True
                            except Exception:
                                pass
                        if has_index:
                            indexed_count += 1

                    # Compute overall progress
                    if indexed_count == total_parts:
                        tracked_stage = 'indexed'   # 80%
                    elif indexed_count > 0:
                        # Between 60% and 80%: some parts indexed
                        tracked_percent = 60 + (indexed_count / total_parts) * 20
                        tracked_stage = 'families'
                    elif chunked_count == total_parts:
                        tracked_stage = 'families'  # 60%
                    elif chunked_count > 0:
                        # Between 40% and 60%: some chunked, none indexed
                        tracked_percent = 40 + (chunked_count / total_parts) * 20
                        tracked_stage = 'chunked'
                    else:
                        tracked_stage = 'uploaded'  # 20%

                    # 'tested' cannot be inferred from chunk data — it requires explicit
                    # user approval. If DB stage map records 'tested' for the parent stem
                    # or any split part, honour it (highest stage wins).
                    db_parent_stage = _stage_map_lookup(db_map, tracked_pdf, tracked_pdf + '.pdf')
                    if db_parent_stage == 'tested':
                        tracked_stage = 'tested'
                        tracked_percent = None
                    else:
                        for p in split_part_files:
                            part_stem = Path(p).stem
                            if _stage_map_lookup(db_map, p, part_stem) == 'tested':
                                tracked_stage = 'tested'
                                tracked_percent = None
                                break

                except Exception:
                    pass

    if tracked_pdf and tracked_stage:
        stats['tracked_pdf']   = tracked_pdf
        stats['tracked_stage'] = tracked_stage
        if tracked_percent is not None:
            stats['tracked_percent']   = round(tracked_percent, 2)
            stats['total_split_parts'] = total_parts
        # NEW: Compute families stage progress (40% → 60% based on approved families)
        elif tracked_stage in ('chunked', 'families'):
            # Check if families exist for this PDF - compute proportional progress
            try:
                from .models import CatalogDocument, ProductFamily
                import re as _re3
                split_re3 = _re3.compile(r'_(custom_)?p\d{4}-\d{4}$', _re3.I)
                docs_for_families = []
                
                # Try to find document(s) for this PDF
                doc_lookup = CatalogDocument.objects.filter(
                    original_filename__in=[tracked_pdf, tracked_pdf.replace('.pdf', '')]
                ).first()
                
                if not doc_lookup:
                    # Check if this is a parent stem with splits
                    splits_dir_check = splits_dir / tracked_pdf
                    if splits_dir_check.exists():
                        split_part_files_check = sorted(splits_dir_check.glob('*.pdf'))
                        if split_part_files_check:
                            split_stems_check = [p.stem for p in split_part_files_check]
                            docs_for_families = list(CatalogDocument.objects.filter(
                                original_filename__in=split_stems_check
                            ))
                else:
                    docs_for_families = [doc_lookup]
                
                if docs_for_families:
                    total_fam = 0
                    approved_fam = 0
                    for doc_fam in docs_for_families:
                        all_fam = ProductFamily.objects.filter(document=doc_fam)
                        total_fam += all_fam.count()
                        approved_fam += all_fam.filter(review_status=ProductFamily.ReviewStatus.APPROVED).count()
                    
                    if total_fam > 0:
                        # Proportional progress: 40% + (approved/total * 20%)
                        families_percent = 40 + (approved_fam / total_fam) * 20
                        stats['tracked_percent'] = round(families_percent, 2)
                        stats['families_progress'] = {
                            'total': total_fam,
                            'approved': approved_fam,
                        }
            except Exception:
                pass
        import logging
        logging.warning(f'[STATS] tracked_pdf={tracked_pdf} tracked_stage={tracked_stage} tracked_percent={tracked_percent} total_parts={total_parts}')

    return JsonResponse(stats)


# ── API: PDF page count ────────────────────────────────────────────────

_page_count_cache = {}

@login_required
def pdf_page_count(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    filename = request.GET.get('pdf', '').strip()
    if not filename:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)
    pdf_path, pdf_bytes, _doc = _resolve_pdf_source(filename)
    if not (pdf_path and pdf_path.exists()) and not pdf_bytes:
        return JsonResponse({'error': 'File not found.'}, status=404)

    cache_key = f"{filename}:{pdf_path.stat().st_mtime}" if pdf_path and pdf_path.exists() else f"{filename}:binary:{len(pdf_bytes or b'')}"
    if cache_key in _page_count_cache:
        return JsonResponse({'pages': _page_count_cache[cache_key], 'filename': filename})

    try:
        from pypdf import PdfReader
        if pdf_path and pdf_path.exists():
            pages = len(PdfReader(str(pdf_path)).pages)
        else:
            from io import BytesIO
            pages = len(PdfReader(BytesIO(pdf_bytes)).pages)
        _page_count_cache[cache_key] = pages
        return JsonResponse({'pages': pages, 'filename': filename})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Split PDF into page-range sub-PDFs ─────────────────────────────

@login_required
@require_POST
def split_pdf(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body      = json.loads(request.body)
        filename  = body.get('filename', '').strip()
        pages_per = int(body.get('pages_per', 10))
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not filename or pages_per < 1:
        return JsonResponse({'error': 'Invalid parameters.'}, status=400)

    pdf_path, pdf_bytes, _doc = _resolve_pdf_source(filename)
    if not (pdf_path and pdf_path.exists()) and not pdf_bytes:
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from pypdf import PdfReader, PdfWriter
        if pdf_path and pdf_path.exists():
            reader = PdfReader(str(pdf_path))
            stem = pdf_path.stem
        else:
            from io import BytesIO
            reader = PdfReader(BytesIO(pdf_bytes))
            stem = Path(filename).stem
        total    = len(reader.pages)
        out_dir  = PROJECT_ROOT / 'input' / 'splits' / stem
        out_dir.mkdir(parents=True, exist_ok=True)

        # Clear old splits for this PDF
        for old in out_dir.glob('*.pdf'):
            old.unlink()

        splits = []
        for start in range(0, total, pages_per):
            end    = min(start + pages_per, total)
            writer = PdfWriter()
            for i in range(start, end):
                writer.add_page(reader.pages[i])
            part_name = f'{stem}_p{start+1:04d}-{end:04d}.pdf'
            part_path = out_dir / part_name
            with open(part_path, 'wb') as f:
                writer.write(f)
            splits.append({'filename': part_name, 'pages': f'{start+1}–{end}', 'page_count': end - start})

        return JsonResponse({
            'message': f'Split into {len(splits)} parts ({pages_per} pages each).',
            'splits': splits,
            'total_pages': total,
            'stem': stem,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Run pipeline on a split part ─────────────────────────────────

@login_required
@require_POST
def run_pipeline_split(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body      = json.loads(request.body)
        stem      = body.get('stem', '').strip()      # original PDF name
        part_file = body.get('part_file', '').strip() # e.g. GGH1_p0001-0010.pdf
        parsing_instructions = _clean_parsing_instructions(body.get('parsing_instructions'))
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = PROJECT_ROOT / 'input' / 'splits' / stem / part_file
    if not pdf_path.exists():
        return JsonResponse({'error': f'Split file not found: {part_file}'}, status=404)

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.gemini_api_key:
            return JsonResponse({'error': 'Gemini API key is not configured. Open Models & Keys.'}, status=400)
        _key = runtime.gemini_api_key
        _provider = 'gemini' if runtime.vision_model.startswith('gemini') else 'openai'
        _active_key = runtime.gemini_api_key if _provider == 'gemini' else runtime.openai_api_key
        logger.warning('[pipeline-split] provider=%s model=%s key=%s...%s part=%s',
                    _provider, runtime.vision_model, _active_key[:6], _active_key[-4:], part_file)
        env = subprocess_environment()
        if parsing_instructions:
            env['PDF_PARSING_INSTRUCTIONS'] = parsing_instructions
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=600,
            env=env,
        )
        output = result.stdout or ''
        logger.info('[pipeline-split] part=%s rc=%d\n%s', part_file, result.returncode, output)
        if result.returncode == 1:
            logger.error('[pipeline-split] FAILED part=%s rc=%d\n%s', part_file, result.returncode, output[-3000:])
            return JsonResponse({
                'error': _clean_pipeline_error(output),
                'detail': output[-4000:],
                'output': output[-4000:],
            }, status=500)
        if result.returncode == 2:
            logger.error('[pipeline-split] API failure part=%s\n%s', part_file, output[-3000:])
            return JsonResponse({
                'error': _clean_pipeline_error(output),
                'detail': output[-4000:],
                'output': output[-4000:],
            }, status=500)
        if result.returncode == 3:
            logger.warning('[pipeline-split] No products extracted part=%s\n%s', part_file, output)
            return JsonResponse({
                'error': 'No products could be extracted from this PDF part. All pages failed validation.',
                'detail': output,
                'output': output,
            }, status=422)

        # Only mark as chunked if pipeline actually ingested products into DB
        no_products = 'No products extracted' in output or 'nothing written to Postgres' in output
        if no_products:
            logger.warning('[pipeline-split] SUCCESS but 0 products extracted part=%s', part_file)
            return JsonResponse({
                'message': f'{part_file} processed but no products were extracted.',
                'output': output[-2000:],
                'no_products': True,
            })

        # Update stage for this split part in session + DB stage map
        import json as _j
        from .models import ApiKey
        try:
            _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            _stage_map = {}
        _stage_map[part_file] = 'chunked'

        # Also update parent stem stage proportionally
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
        all_parts = sorted(p.name for p in splits_dir.glob('*.pdf')) if splits_dir.exists() else [part_file]
        done_parts = [p for p in all_parts if _stage_map.get(p) in ('chunked', 'families', 'indexed', 'tested')]
        if len(done_parts) == len(all_parts):
            _stage_map[stem] = 'chunked'
        ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})

        pdf_progress = request.session.get('pdf_progress', {})
        pdf_progress[part_file] = 'chunked'
        if len(done_parts) == len(all_parts):
            pdf_progress[stem] = 'chunked'
        request.session['pdf_progress'] = pdf_progress
        request.session.modified = True

        logger.warning('[pipeline-split] SUCCESS provider=%s model=%s part=%s', _provider, runtime.vision_model, part_file)
        return JsonResponse({'message': f'{part_file} processed.', 'output': output[-2000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out.'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Split PDF into custom page ranges ──────────────────────────────

@login_required
@require_POST
def split_pdf_custom(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body     = json.loads(request.body)
        filename = body.get('filename', '').strip()
        ranges   = body.get('ranges', [])  # [{start:1, end:3}, {start:4, end:5}, ...]
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not filename or not ranges:
        return JsonResponse({'error': 'Invalid parameters.'}, status=400)

    pdf_path, pdf_bytes, _doc = _resolve_pdf_source(filename)
    if not (pdf_path and pdf_path.exists()) and not pdf_bytes:
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from pypdf import PdfReader, PdfWriter
        if pdf_path and pdf_path.exists():
            reader = PdfReader(str(pdf_path))
            stem = pdf_path.stem
        else:
            from io import BytesIO
            reader = PdfReader(BytesIO(pdf_bytes))
            stem = Path(filename).stem
        total   = len(reader.pages)
        out_dir = PROJECT_ROOT / 'input' / 'splits' / stem
        out_dir.mkdir(parents=True, exist_ok=True)

        for old in out_dir.glob('*.pdf'):
            old.unlink()

        splits = []
        for i, r in enumerate(ranges):
            start = int(r.get('start', 1))
            end   = int(r.get('end', start))
            if start < 1 or end > total or start > end:
                return JsonResponse({'error': f'Invalid range {start}–{end} (PDF has {total} pages).'}, status=400)
            writer = PdfWriter()
            for p in range(start - 1, end):
                writer.add_page(reader.pages[p])
            part_name = f'{stem}_custom_p{start:04d}-{end:04d}.pdf'
            part_path = out_dir / part_name
            with open(part_path, 'wb') as f:
                writer.write(f)
            splits.append({'filename': part_name, 'pages': f'{start}–{end}', 'page_count': end - start + 1})

        return JsonResponse({
            'message': f'Split into {len(splits)} custom parts.',
            'splits': splits,
            'total_pages': total,
            'stem': stem,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# -- API: Delete PDF + all associated data ------------------------------------

@login_required
@require_POST
def delete_pdf(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body     = json.loads(request.body)
        filename = body.get('filename', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)
    if not filename:
        return JsonResponse({'error': 'Missing filename.'}, status=400)

    import shutil
    import re
    
    stem = Path(filename).stem
    # Use EXACT same sanitization as vision_pipeline/main.py _pdf_slug function
    sanitized_stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', stem)[:60].strip('_') or 'catalog'
    
    # Check if this is a split PDF by looking for pattern: ParentName_[custom_]pXXXX-XXXX
    is_split = bool(re.search(r'_(custom_)?p\d{4}-\d{4}$', stem))
    
    if is_split:
        # Find and delete the actual PDF file (may be nested at any depth)
        split_pdf_path = _resolve_pdf_path(filename)
        actual_parent_dir = split_pdf_path.parent if split_pdf_path.exists() else None
        if split_pdf_path.exists():
            split_pdf_path.unlink()

        # Also delete the PDF's own splits subdir if it exists (intermediate split)
        own_splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
        if own_splits_dir.exists():
            shutil.rmtree(own_splits_dir)

        # Walk up: clean any parent splits dir that is now empty of leaf PDFs
        splits_root = PROJECT_ROOT / 'input' / 'splits'
        if actual_parent_dir and actual_parent_dir != splits_root and actual_parent_dir.exists():
            remaining = list(actual_parent_dir.rglob('*.pdf'))
            if not remaining:
                shutil.rmtree(actual_parent_dir)
                # Also delete the grandparent intermediate PDF if its dir is now empty
                grandparent_stem = actual_parent_dir.name
                grandparent_pdf = splits_root / grandparent_stem.rsplit('_', 1)[0] if '_' in grandparent_stem else None
                parent_pdf = PROJECT_ROOT / 'input' / f'{actual_parent_dir.name}.pdf'
                if parent_pdf.exists():
                    parent_pdf.unlink()
    else:
        # Delete main PDF file
        pdf_path = PROJECT_ROOT / 'input' / filename
        if pdf_path.exists():
            pdf_path.unlink()
        
        # Delete entire split directory if this was a parent PDF
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
        if splits_dir.exists():
            shutil.rmtree(splits_dir)

    # Delete data directories - vision pipeline uses sanitized stem
    vision_data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    if vision_data_dir.exists():
        for folder in vision_data_dir.iterdir():
            if folder.is_dir() and (
                folder.name == sanitized_stem or 
                folder.name.startswith(sanitized_stem + '_')
            ):
                try:
                    shutil.rmtree(folder)
                except Exception:
                    pass

    # Delete from Qdrant vector store
    try:
        from qdrant_client import models
        from rag_pipeline.providers import COLLECTION, build_qdrant_client
        client = build_qdrant_client()
        if client.collection_exists(COLLECTION):
            # Try every source_pdf spelling used by legacy and V2 paths.
            source_pdf_values = list(dict.fromkeys([
                filename,
                stem,
                sanitized_stem,
                f'{stem}.pdf',
                f'{sanitized_stem}.pdf',
            ]))
            for source_stem in source_pdf_values:
                try:
                    client.delete(
                        collection_name=COLLECTION,
                        points_selector=models.FilterSelector(
                            filter=models.Filter(
                                must=[
                                    models.FieldCondition(
                                        key='metadata.source_pdf',
                                        match=models.MatchValue(value=source_stem),
                                    )
                                ]
                            )
                        ),
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Delete from V2 database
    deleted_documents = 0
    try:
        from .models import CatalogDocument
        from .services.documents import archive_document
        from django.db.models import Q
        
        filename_candidates = {
            filename,
            stem,
            f'{stem}.pdf',
            sanitized_stem,
            f'{sanitized_stem}.pdf',
        }
        document_filter = Q(original_filename__in=filename_candidates)
        if not is_split:
            document_filter |= Q(original_filename__startswith=stem + '_')
            document_filter |= Q(original_filename__startswith=sanitized_stem + '_')

        docs_to_delete = list(
            CatalogDocument.objects.filter(document_filter)
            .distinct()
            .order_by('original_filename', 'id')
        )
        for doc in docs_to_delete:
            try:
                archive_document(doc)
            except Exception:
                pass
            try:
                if doc.file:
                    doc.file.delete(save=False)
            except Exception:
                pass
            try:
                doc.delete()
                deleted_documents += 1
            except Exception:
                pass
                
    except Exception as e:
        import logging
        logging.warning(f'Error deleting V2 database records for {filename}: {e}')

    # Clear stage map entries for this PDF and its split parts
    import json as _j
    from .models import ApiKey
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        keys_to_remove = [k for k in _stage_map if k == filename or k == stem or k.startswith(stem + '_')]
        for k in keys_to_remove:
            del _stage_map[k]
        ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})
    except Exception:
        pass

    # Clear session stage for this PDF
    pdf_progress = request.session.get('pdf_progress', {})
    approved_pdfs = request.session.get('approved_pdfs', [])
    keys_to_remove = [k for k in pdf_progress if k == filename or k == stem or k.startswith(stem + '_')]
    for k in keys_to_remove:
        del pdf_progress[k]
    approved_pdfs = [p for p in approved_pdfs if p != filename and p != stem and not p.startswith(stem + '_')]
    request.session['pdf_progress'] = pdf_progress
    request.session['approved_pdfs'] = approved_pdfs
    request.session.modified = True

    return JsonResponse({
        'message': f'"{filename}" and all associated data deleted.',
        'deleted_documents': deleted_documents,
    })


# -- API: Delete embeddings only (Create Chunks panel) ────────────────────────

@login_required
@require_POST
def delete_embeddings_only(request):
    """Delete only Qdrant embeddings — keep PDF files, chunks, and DB records intact."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body     = json.loads(request.body)
        filename = body.get('filename', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)
    if not filename:
        return JsonResponse({'error': 'Missing filename.'}, status=400)

    import re
    stem = Path(filename).stem
    sanitized_stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', stem)[:60].strip('_') or 'catalog'

    # Collect all stems to process: include split children at every nesting level
    all_stems = [stem]
    splits_root = PROJECT_ROOT / 'input' / 'splits'
    splits_dir = splits_root / stem
    if splits_dir.exists():
        # Standard case: parent has its own subdir
        all_stems.extend(p.stem for p in sorted(splits_dir.rglob('*.pdf')))
        all_stems.extend(sub.name for sub in splits_dir.rglob('*') if sub.is_dir())
    else:
        # Flat case: split parts live as top-level dirs in splits_root named <stem>_p...
        # e.g. Hand_Tool_Catalogue_low_res_with_cover_p0146-0150/
        if splits_root.exists():
            for part_dir in sorted(splits_root.iterdir()):
                if part_dir.is_dir() and part_dir.name.startswith(stem + '_'):
                    all_stems.append(part_dir.name)  # intermediate stem
                    all_stems.extend(p.stem for p in sorted(part_dir.rglob('*.pdf')))

    try:
        from qdrant_client import models
        from rag_pipeline.providers import COLLECTION, build_qdrant_client
        client = build_qdrant_client()
        if client.collection_exists(COLLECTION):
            for s in all_stems:
                san = re.sub(r'[^a-zA-Z0-9_\-]', '_', s)[:60].strip('_') or 'catalog'
                for source_stem in {s, san}:
                    try:
                        client.delete(
                            collection_name=COLLECTION,
                            points_selector=models.FilterSelector(
                                filter=models.Filter(
                                    must=[models.FieldCondition(
                                        key='metadata.source_pdf',
                                        match=models.MatchValue(value=source_stem),
                                    )]
                                )
                            ),
                        )
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        from .models import CatalogDocument, DocumentChunk
        docs = CatalogDocument.objects.filter(original_filename__in=all_stems)
        DocumentChunk.objects.filter(document__in=docs).update(
            index_status=DocumentChunk.IndexStatus.PENDING
        )
    except Exception as e:
        import logging
        logging.warning(f'Error resetting chunk index status for {filename}: {e}')

    # Downgrade stage from indexed/tested → chunked for parent and all children
    import json as _j
    from .models import ApiKey
    all_keys = list(dict.fromkeys(all_stems + [s + '.pdf' for s in all_stems]))
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        for key in all_keys:
            if _stage_map.get(key) in ('indexed', 'tested'):
                _stage_map[key] = 'chunked'
        ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})
    except Exception:
        pass

    # Also downgrade session stage
    pdf_progress = request.session.get('pdf_progress', {})
    for key in all_keys:
        if pdf_progress.get(key) in ('indexed', 'tested'):
            pdf_progress[key] = 'chunked'
    request.session['pdf_progress'] = pdf_progress
    request.session.modified = True

    return JsonResponse({'message': f'Embeddings deleted for "{filename}". Chunks and data remain intact.'})


# -- API: List chunks for a PDF ────────────────────────────────────────────────

def _parse_ordinal_from_filename(filename: str) -> int:
    import re
    match = re.match(r'^(\d+)_', filename)
    if match:
        return int(match.group(1))
    match = re.match(r'^chunk_(\d+)', filename)
    if match:
        return int(match.group(1))
    match = re.search(r'(\d+)', filename)
    if match:
        return int(match.group(1))
    return 0


@login_required
def list_chunks(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    pdf_name = request.GET.get('pdf', '').strip()
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)

    import re
    chunks = []
    from .models import CatalogDocument, DocumentChunk

    pdf_stem = Path(pdf_name).stem
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
    index_only = request.GET.get('index') == '1'  # True = Index & Embed panel, exclude STALE
    doc = _resolve_catalog_document(pdf_name)

    document_id = str(doc.id) if doc else None

    docs_to_query = []
    if not doc or not split_re.search(pdf_stem):
        split_docs = _resolve_split_docs_for_parent(pdf_stem)
        if split_docs:
          docs_to_query = split_docs

    if not docs_to_query:
        if not doc:
            return JsonResponse({
                'error': f'PDF document not found for "{pdf_name}". Please ensure chunks have been created for this PDF.'
            }, status=404)
        docs_to_query = [doc]

    if not doc:
        doc = docs_to_query[0]
        document_id = str(doc.id)

    if len(docs_to_query) > 1:
        document_id = str(docs_to_query[0].id)
        ordinal_offset = 0
        for split_doc in docs_to_query:
            qs = DocumentChunk.objects.filter(document=split_doc)
            if index_only:
                qs = qs.exclude(index_status=DocumentChunk.IndexStatus.STALE)
            for c in qs.order_by('ordinal'):
                family = c.family
                prod_name = 'General Info'
                if family:
                    prod_name = family.product_name
                elif c.variant and c.variant.family:
                    prod_name = c.variant.family.product_name
                chunks.append({
                    'id': str(c.id),
                    'filename': f"chunk_{ordinal_offset + c.ordinal:04d}.md",
                    'content': c.text,
                    'excerpt': _chunk_excerpt(c.text),
                    'ordinal': ordinal_offset + c.ordinal,
                    'product_name': prod_name,
                    'family_id': str(c.family_id) if c.family_id else '',
                    'family_name': family.product_name if family else '',
                    'family_code': family.product_code if family else '',
                    'family_status': family.review_status if family else '',
                    'page_start': c.page_start,
                    'page_end': c.page_end,
                    'status': 'Embedded' if c.index_status == 'indexed' else 'Ready',
                })
            ordinal_offset += qs.count()
        return JsonResponse({'document_id': document_id, 'chunks': chunks})

    qs = DocumentChunk.objects.filter(document=doc)
    if index_only:
        qs = qs.exclude(index_status=DocumentChunk.IndexStatus.STALE)
    db_chunks = qs.order_by('ordinal')
    for c in db_chunks:
        family = c.family
        prod_name = 'General Info'
        if family:
            prod_name = family.product_name
        elif c.variant and c.variant.family:
            prod_name = c.variant.family.product_name
        chunks.append({
            'id': str(c.id),
            'filename': f"chunk_{c.ordinal:04d}.md",
            'content': c.text,
            'excerpt': _chunk_excerpt(c.text),
            'ordinal': c.ordinal,
            'product_name': prod_name,
            'family_id': str(c.family_id) if c.family_id else '',
            'family_name': family.product_name if family else '',
            'family_code': family.product_code if family else '',
            'family_status': family.review_status if family else '',
            'page_start': c.page_start,
            'page_end': c.page_end,
            'status': 'Embedded' if c.index_status == 'indexed' else 'Ready',
        })
    return JsonResponse({'document_id': document_id, 'chunks': chunks})


def _serialize_family_editor_chunk(chunk):
    family = chunk.family
    return {
        'id': str(chunk.id),
        'ordinal': chunk.ordinal,
        'filename': f'chunk_{chunk.ordinal:04d}.md',
        'product_name': family.product_name if family else (chunk.variant.family.product_name if chunk.variant and chunk.variant.family else 'General Info'),
        'product_code': family.product_code if family else '',
        'family_id': str(family.id) if family else '',
        'family_name': family.product_name if family else '',
        'family_code': family.product_code if family else '',
        'family_status': family.review_status if family else '',
        'page_start': chunk.page_start,
        'page_end': chunk.page_end,
        'status': 'Embedded' if chunk.index_status == DocumentChunk.IndexStatus.INDEXED else 'Ready',
        'content': chunk.text,
        'excerpt': _chunk_excerpt(chunk.text),
    }


def _serialize_family_editor_family(family):
    chunks = list(family.chunks.all().order_by('ordinal'))
    return {
        'id': str(family.id),
        'product_name': family.product_name,
        'product_code': family.product_code,
        'raw_category': family.raw_category,
        'aliases': family.aliases or [],
        'category': family.normalized_category.name if family.normalized_category else family.raw_category,
        'review_status': family.review_status,
        'page_start': family.page_start,
        'page_end': family.page_end,
        'chunk_count': len(chunks),
        'variants': [
            {
                'id': str(variant.id),
                'variant_id': str(variant.id),
                'product_code': variant.product_code,
                'order_number': variant.order_number,
                'name': variant.name,
                'size': variant.size,
                'unit': variant.unit,
                'specifications': variant.specifications,
                'ordering_data': variant.ordering_data,
            }
            for variant in family.variants.all().order_by('product_code', 'order_number', 'name', 'id')
        ],
        'chunks': [
            {
                'id': str(chunk.id),
                'ordinal': chunk.ordinal,
                'filename': f'chunk_{chunk.ordinal:04d}.md',
                'excerpt': _chunk_excerpt(chunk.text),
                'page_start': chunk.page_start,
                'page_end': chunk.page_end,
            }
            for chunk in chunks
        ],
    }


def _clean_family_aliases(value):
    if isinstance(value, str):
        raw_values = re.split(r'[,\n;]+', value)
    elif isinstance(value, list):
        raw_values = value
    else:
        raw_values = []
    aliases = []
    seen = set()
    for raw in raw_values:
        alias = re.sub(r'\s+', ' ', str(raw or '').strip())
        key = alias.lower()
        if alias and key not in seen:
            aliases.append(alias[:120])
            seen.add(key)
    return aliases[:20]


def _clean_family_variants(value):
    if not isinstance(value, list):
        return []
    variants = []
    seen = set()
    for raw in value[:50]:
        if not isinstance(raw, dict):
            continue
        variant_id = str(raw.get('variant_id') or raw.get('id') or '').strip()
        product_code = re.sub(r'\s+', ' ', str(raw.get('product_code') or '').strip())[:160]
        order_number = re.sub(r'\s+', ' ', str(raw.get('order_number') or '').strip())[:160]
        name = re.sub(r'\s+', ' ', str(raw.get('name') or '').strip())[:500]
        size = re.sub(r'\s+', ' ', str(raw.get('size') or '').strip())[:255]
        unit = re.sub(r'\s+', ' ', str(raw.get('unit') or '').strip())[:80]
        specifications = _clean_variant_json(raw.get('specifications'))
        ordering_data = _clean_variant_json(raw.get('ordering_data'))
        if not any((product_code, order_number, name, size, unit)) and not specifications and not ordering_data:
            continue
        key = json.dumps({
            'product_code': product_code,
            'order_number': order_number,
            'name': name,
            'size': size,
            'unit': unit,
            'specifications': specifications,
            'ordering_data': ordering_data,
        }, sort_keys=True, ensure_ascii=True)
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            'variant_id': variant_id,
            'product_code': product_code,
            'order_number': order_number,
            'name': name,
            'size': size,
            'unit': unit,
            'specifications': specifications,
            'ordering_data': ordering_data,
        })
    return variants


def _clean_variant_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _variant_row_hash(variant):
    import hashlib

    payload = json.dumps(variant, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


@login_required
def list_product_families(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    pdf_name = request.GET.get('pdf', '').strip()
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)

    from django.db.models import Prefetch
    from .models import DocumentChunk, ProductFamily, CatalogDocument
    import re

    pdf_stem = Path(pdf_name).stem
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)

    doc = _resolve_catalog_document(pdf_name)

    # Build docs_to_query: for parent stems with no direct DB doc, resolve via split parts
    docs_to_query = []
    if not doc or (not split_re.search(pdf_stem)):
        # Try both layout 1 (splits/<stem>/) and layout 2 (splits/<stem>_p*/ siblings)
        split_docs = _resolve_split_docs_for_parent(pdf_stem)
        if split_docs:
            docs_to_query = split_docs

    if not docs_to_query:
        if not doc:
            import logging
            logging.error(f"list_product_families: PDF not found for name '{pdf_name}'. Checked stems and splits.")
            return JsonResponse({'error': f'PDF document not found for "{pdf_name}". Please ensure chunks have been created for this PDF.'}, status=404)
        docs_to_query = [doc]

    if not doc:
        doc = docs_to_query[0]

    # Aggregate chunks from all documents
    all_chunks = []
    ordinal_offset = 0
    for current_doc in docs_to_query:
        doc_chunks = DocumentChunk.objects.filter(document=current_doc).select_related(
            'family',
            'family__normalized_category',
            'variant',
            'variant__family',
        ).order_by('ordinal')
        
        for chunk in doc_chunks:
            serialized = _serialize_family_editor_chunk(chunk)
            serialized['ordinal'] = ordinal_offset + chunk.ordinal
            serialized['filename'] = f'chunk_{ordinal_offset + chunk.ordinal:04d}.md'
            all_chunks.append(serialized)
        
        ordinal_offset += doc_chunks.count()

    # Aggregate families from all documents
    all_families = []
    seen_family_ids = set()
    for current_doc in docs_to_query:
        doc_families = ProductFamily.objects.filter(document=current_doc).select_related(
            'normalized_category',
        ).prefetch_related(
            Prefetch('chunks', queryset=DocumentChunk.objects.select_related('family').order_by('ordinal')),
            'variants',
        ).order_by('product_name', 'product_code', 'id')
        
        for family in doc_families:
            if family.id not in seen_family_ids:
                all_families.append(_serialize_family_editor_family(family))
                seen_family_ids.add(family.id)

    return JsonResponse({
        'document_id': str(doc.id),
        'pdf': pdf_stem if len(docs_to_query) > 1 else doc.original_filename,
        'status': doc.status,
        'is_active': doc.is_active,
        'chunk_count': len(all_chunks),
        'family_count': len(all_families),
        'chunks': all_chunks,
        'families': all_families,
    })


@login_required
def product_families_approval_status(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    pdf_name = request.GET.get('pdf', '').strip()
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)

    from .models import ProductFamily, CatalogDocument
    import re

    doc = _resolve_catalog_document(pdf_name)
    if not doc:
        return JsonResponse({'error': f'PDF document not found for "{pdf_name}".'}, status=404)

    pdf_stem = Path(pdf_name).stem
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
    docs_to_query = [doc]

    if split_re.search(doc.original_filename) or not split_re.search(pdf_stem):
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / pdf_stem
        if splits_dir.exists():
            split_stems = [p.stem for p in sorted(splits_dir.glob('*.pdf'))]
            split_docs = list(CatalogDocument.objects.filter(original_filename__in=split_stems).order_by('original_filename'))
            if split_docs:
                docs_to_query = split_docs

    families = []
    for current_doc in docs_to_query:
        for fam in ProductFamily.objects.filter(document=current_doc).order_by('product_name', 'product_code'):
            families.append({
                'id': str(fam.id),
                'product_name': fam.product_name,
                'product_code': fam.product_code,
                'review_status': fam.review_status,
                'page_start': fam.page_start,
                'page_end': fam.page_end,
            })

    total = len(families)
    approved = sum(1 for f in families if f['review_status'] == ProductFamily.ReviewStatus.APPROVED)
    needs_review = sum(1 for f in families if f['review_status'] == ProductFamily.ReviewStatus.NEEDS_REVIEW)
    rejected = sum(1 for f in families if f['review_status'] == ProductFamily.ReviewStatus.REJECTED)

    return JsonResponse({
        'pdf': pdf_name,
        'total': total,
        'approved': approved,
        'needs_review': needs_review,
        'rejected': rejected,
        'families': families,
    })


@login_required
@require_POST
def save_product_family(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_name = str(body.get('pdf', '')).strip()
    product_name = str(body.get('product_name', '')).strip()
    product_code = re.sub(r'\s+', ' ', str(body.get('product_code', '')).strip())[:160]
    raw_category = str(body.get('raw_category', '')).strip()
    aliases = _clean_family_aliases(body.get('aliases', []))
    variants = _clean_family_variants(body.get('variants', []))
    review_status = str(body.get('review_status', 'approved')).strip().lower()
    family_id = str(body.get('family_id', '')).strip()
    chunk_ids = body.get('chunk_ids', [])
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)
    if not product_name:
        return JsonResponse({'error': 'Product name is required.'}, status=400)
    if review_status not in {ProductFamily.ReviewStatus.APPROVED, ProductFamily.ReviewStatus.NEEDS_REVIEW, ProductFamily.ReviewStatus.REJECTED}:
        return JsonResponse({'error': 'Invalid review status.'}, status=400)
    if not isinstance(chunk_ids, list):
        return JsonResponse({'error': 'chunk_ids must be an array.'}, status=400)

    chunk_ids = [str(chunk_id).strip() for chunk_id in chunk_ids if str(chunk_id).strip()]
    if not chunk_ids:
        return JsonResponse({'error': 'Select at least one chunk to save a product.'}, status=400)
    doc = _resolve_catalog_document(pdf_name)
    if not doc:
        return JsonResponse({'error': 'PDF not found.'}, status=404)

    from django.db import transaction
    from django.db.models import Prefetch
    from django.utils import timezone
    from .models import ProductVariant
    from .services.taxonomy import resolve_category

    with transaction.atomic():
        if family_id:
            family = ProductFamily.objects.select_for_update().filter(id=family_id).first()
            if not family:
                return JsonResponse({'error': 'Product not found.'}, status=404)
            # Use the family's actual document for chunk lookups
            doc = family.document
            current_family_chunks = list(
                DocumentChunk.objects.select_for_update().filter(
                    family=family,
                )
            )
        else:
            family = ProductFamily(
                document=doc,
                source_key=f'ui:{uuid.uuid4()}',
            )
            current_family_chunks = []

        chunks = []
        if chunk_ids:
            chunks = list(
                DocumentChunk.objects.select_for_update().filter(
                    id__in=chunk_ids,
                )
            )
            if len(chunks) != len(chunk_ids):
                return JsonResponse({'error': 'One or more chunks were not found.'}, status=404)
        selected_chunk_ids = {chunk.id for chunk in chunks}

        category = resolve_category(
            product_name=product_name,
            raw_category=raw_category,
        )
        family.product_name = product_name
        if product_code or not family_id:
            family.product_code = product_code
        family.raw_category = raw_category
        family.aliases = aliases
        if category is not None or not family_id:
            family.normalized_category = category
        family.review_status = review_status
        computed_page_start = min((chunk.page_start for chunk in chunks if chunk.page_start), default=0)
        computed_page_end = max((chunk.page_end for chunk in chunks if chunk.page_end), default=computed_page_start)
        family.page_start = int(body.get('page_start') or computed_page_start or family.page_start or 0)
        family.page_end = int(body.get('page_end') or computed_page_end or family.page_end or family.page_start or 0)
        family.save()

        locked_variants = list(family.variants.select_for_update().all())
        existing_variants = {
            str(variant.id): variant
            for variant in locked_variants
        }
        existing_variants_by_hash = {
            variant.source_row_hash: variant
            for variant in locked_variants
        }
        for variant in variants:
            variant_id = variant.get('variant_id') or ''
            row_hash = _variant_row_hash(variant)
            existing_variant = existing_variants.get(variant_id) or existing_variants_by_hash.get(row_hash)
            source_row_hash = existing_variant.source_row_hash if existing_variant else row_hash
            if existing_variant:
                existing_variant.product_code = variant['product_code']
                existing_variant.order_number = variant['order_number']
                existing_variant.name = variant['name']
                existing_variant.size = variant['size']
                existing_variant.unit = variant['unit']
                existing_variant.specifications = variant['specifications']
                existing_variant.ordering_data = variant['ordering_data']
                existing_variant.page_start = family.page_start
                existing_variant.page_end = family.page_end
                existing_variant.save(update_fields=(
                    'product_code',
                    'order_number',
                    'name',
                    'size',
                    'unit',
                    'specifications',
                    'ordering_data',
                    'page_start',
                    'page_end',
                    'updated_at',
                ))
                continue
            ProductVariant.objects.create(
                family=family,
                product_code=variant['product_code'],
                order_number=variant['order_number'],
                name=variant['name'],
                size=variant['size'],
                unit=variant['unit'],
                specifications=variant['specifications'],
                ordering_data=variant['ordering_data'],
                page_start=family.page_start,
                page_end=family.page_end,
                source_row_hash=source_row_hash,
            )

        if chunk_ids:
            updated_at = timezone.now()
            for chunk in chunks:
                chunk.family = family
                chunk.updated_at = updated_at
            DocumentChunk.objects.bulk_update(chunks, ('family', 'updated_at'))

            deselected_chunks = [
                chunk for chunk in current_family_chunks
                if chunk.id not in selected_chunk_ids
            ]
            if deselected_chunks:
                for chunk in deselected_chunks:
                    chunk.family = None
                    chunk.updated_at = updated_at
                DocumentChunk.objects.bulk_update(deselected_chunks, ('family', 'updated_at'))

        refresh_chunk_ids = {chunk.id for chunk in current_family_chunks} | selected_chunk_ids
        indexed_chunks = DocumentChunk.objects.filter(
            id__in=refresh_chunk_ids,
            index_status=DocumentChunk.IndexStatus.INDEXED,
        ).select_related(
            'document', 'family', 'family__normalized_category',
            'family__normalized_category__parent', 'variant',
        )
        if indexed_chunks.exists():
            from .services.indexing import refresh_chunk_payloads
            refresh_chunk_payloads(indexed_chunks)

    # Calculate progress based on approved families
    # For split PDFs, aggregate families from ALL split parts
    pdf_stem = Path(pdf_name).stem
    split_re = re.compile(r'_(custom_)?p\d{4}-\d{4}$', re.I)
    docs_to_query = [doc]
    
    if split_re.search(doc.original_filename) or not split_re.search(pdf_stem):
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / pdf_stem
        if splits_dir.exists():
            split_part_files = sorted(splits_dir.glob('*.pdf'))
            if len(split_part_files) > 1:
                split_stems = [p.stem for p in split_part_files]
                from .models import CatalogDocument
                docs_to_query = list(CatalogDocument.objects.filter(
                    original_filename__in=split_stems
                ).order_by('original_filename'))
                if not docs_to_query:
                    docs_to_query = [doc]
    
    # Count total and approved families across all documents
    total_families = 0
    approved_families = 0
    for current_doc in docs_to_query:
        all_families = ProductFamily.objects.filter(document=current_doc)
        total_families += all_families.count()
        approved_families += all_families.filter(review_status=ProductFamily.ReviewStatus.APPROVED).count()
    
    # Update progress: 40% (chunked) → 60% (families approved)
    # Progress = 40% + (approved/total * 20%)
    if total_families > 0:
        families_progress_percent = 40 + (approved_families / total_families) * 20
        
        # Update stage to 'families' with custom percent, or advance to 'families' if all approved
        from .models import ApiKey
        import json as _j
        
        tracked_pdf = pdf_stem if len(docs_to_query) > 1 else pdf_name
        new_stage = 'families' if approved_families > 0 else 'chunked'
        
        # Update DB stage map
        try:
            _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
        except Exception:
            _stage_map = {}
        
        _stage_map[tracked_pdf] = new_stage
        ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})
        
        # Update session
        pdf_progress = request.session.get('pdf_progress', {})
        pdf_progress[tracked_pdf] = new_stage
        request.session['pdf_progress'] = pdf_progress
        request.session.modified = True

    families = [
        _serialize_family_editor_family(item)
        for item in ProductFamily.objects.filter(document=doc).select_related(
            'normalized_category',
        ).prefetch_related(
            Prefetch('chunks', queryset=DocumentChunk.objects.select_related('family').order_by('ordinal')),
            'variants',
        ).order_by('product_name', 'product_code', 'id')
    ]
    chunks = [
        _serialize_family_editor_chunk(chunk)
        for chunk in DocumentChunk.objects.filter(document=doc).select_related(
            'family',
            'family__normalized_category',
            'variant',
            'variant__family',
        ).order_by('ordinal')
    ]
    return JsonResponse({
        'message': f'Product "{product_name}" saved.',
        'family': _serialize_family_editor_family(family),
        'chunks': chunks,
        'families': families,
        'document_id': str(doc.id),
        'progress_info': {
            'total_families': total_families,
            'approved_families': approved_families,
            'progress_percent': round(families_progress_percent, 1) if total_families > 0 else 40,
        }
    })


@login_required
@require_POST
def bulk_approve_families(request):
    """Auto-approve all families for a PDF (used by automation pipeline)."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_name = str(body.get('pdf', '')).strip()
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)

    from .models import ProductFamily
    from django.db import transaction

    pdf_stem = Path(pdf_name).stem
    split_docs = _resolve_split_docs_for_parent(pdf_stem)
    docs = split_docs if split_docs else []
    if not docs:
        doc = _resolve_catalog_document(pdf_name)
        if not doc:
            return JsonResponse({'error': f'PDF not found: {pdf_name}'}, status=404)
        docs = [doc]

    updated = 0
    with transaction.atomic():
        for doc in docs:
            count = ProductFamily.objects.filter(
                document=doc
            ).exclude(
                review_status=ProductFamily.ReviewStatus.APPROVED
            ).update(review_status=ProductFamily.ReviewStatus.APPROVED)
            updated += count

    total = sum(ProductFamily.objects.filter(document=d).count() for d in docs)
    return JsonResponse({'approved': updated, 'total': total})


@login_required
@require_POST
def save_chunk(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        pdf_name = Path(str(body.get('pdf', '')).strip()).name
        chunk_name = Path(str(body.get('filename', '')).strip()).name
        chunk_id = str(body.get('chunk_id', '')).strip()
        content = str(body.get('content', ''))
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not pdf_name or not chunk_name.endswith('.md'):
        return JsonResponse({'error': 'Invalid chunk file.'}, status=400)

    # Save to the database directly
    from .models import CatalogDocument, DocumentChunk
    import hashlib
    
    db_updated = False

    chunk = None
    if chunk_id:
        chunk = DocumentChunk.objects.select_related('document').filter(id=chunk_id).first()

    if chunk is None:
        ordinal = _parse_ordinal_from_filename(chunk_name)
        pdf_stem = Path(pdf_name).stem
        doc = CatalogDocument.objects.filter(original_filename=pdf_stem).order_by('-version').first()
        if not doc:
            doc = CatalogDocument.objects.filter(original_filename=pdf_name).order_by('-version').first()
        if doc:
            chunk = DocumentChunk.objects.filter(document=doc, ordinal=ordinal).first()

    if chunk:
        chunk.text = content
        chunk.content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        chunk.index_status = DocumentChunk.IndexStatus.STALE
        chunk.save(update_fields=['text', 'content_hash', 'index_status', 'updated_at'])
        db_updated = True

    if not db_updated:
        return JsonResponse({'error': 'Chunk database record not found.'}, status=404)

    return JsonResponse({'message': f'{chunk_name} saved.', 'filename': chunk_name})


# ── API: Models & encrypted provider keys ─────────────────────────────────────

@login_required
def get_model_configuration(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .model_config import configuration_payload
    try:
        return JsonResponse(configuration_payload())
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=500)


@login_required
@require_POST
def save_model_configuration(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    from .model_config import update_configuration
    try:
        payload = update_configuration(body, user=request.user)
        payload['message'] = 'Models and API keys saved securely.'
        return JsonResponse(payload)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)


# -- Catalog-aware RAG V2 APIs (feature-flagged) ------------------------------

def _v2_job_payload(job):
    document = job.document
    return {
        'job_id': str(job.id),
        'document_id': str(document.id),
        'catalog_id': str(document.catalog_id),
        'filename': document.original_filename,
        'version': document.version,
        'source_type': document.source_type,
        'document_status': document.status,
        'job_stage': job.stage,
        'job_status': job.status,
        'completed_units': job.completed_units,
        'total_units': job.total_units,
        'retry_count': job.retry_count,
        'cancel_requested': job.cancel_requested,
        'error': job.error_summary,
    }


@login_required
@require_POST
def upload_pdf_v2(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)

    uploaded = request.FILES.get('pdf')
    if not uploaded:
        return JsonResponse({'error': 'A PDF file is required.'}, status=400)

    from .models import Catalog, CatalogDocument
    from .services.documents import DuplicateDocumentError, create_catalog_document

    catalog = None
    catalog_id = request.POST.get('catalog_id', '').strip()
    if catalog_id:
        try:
            catalog = Catalog.objects.get(pk=catalog_id)
        except (Catalog.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Catalog not found.'}, status=404)

    try:
        document, job = create_catalog_document(
            uploaded_file=uploaded,
            user=request.user,
            catalog=catalog,
            catalog_name=request.POST.get('catalog_name', ''),
            source_type=request.POST.get('source_type', CatalogDocument.SourceType.CATALOG),
        )
    except DuplicateDocumentError as exc:
        return JsonResponse({
            'error': 'This exact PDF has already been uploaded.',
            'document_id': str(exc.document.id),
            'status': exc.document.status,
        }, status=409)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception as exc:
        return JsonResponse({'error': f'Could not register PDF: {exc}'}, status=500)

    payload = _v2_job_payload(job)
    payload['message'] = 'PDF registered. The V2 worker can now process the pending job.'
    return JsonResponse(payload, status=201)


@login_required
def ingestion_job_status_v2(request, job_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .models import IngestionJob

    try:
        job = IngestionJob.objects.select_related('document').get(pk=job_id)
    except (IngestionJob.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Ingestion job not found.'}, status=404)
    return JsonResponse(_v2_job_payload(job))


@login_required
@require_POST
def retry_ingestion_job_v2(request, job_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)

    from django.db import transaction
    from .models import CatalogDocument, IngestionJob

    try:
        with transaction.atomic():
            job = IngestionJob.objects.select_for_update().select_related('document').get(pk=job_id)
            if job.status not in (IngestionJob.Status.FAILED, IngestionJob.Status.CANCELLED):
                return JsonResponse({'error': f'Job cannot be retried from status {job.status}.'}, status=409)
            job.status = IngestionJob.Status.PENDING
            job.stage = IngestionJob.Stage.UPLOAD
            job.retry_count += 1
            job.cancel_requested = False
            job.error_summary = ''
            job.completed_at = None
            job.save()
            job.document.status = CatalogDocument.Status.UPLOADED
            job.document.failure_summary = ''
            job.document.save(update_fields=('status', 'failure_summary', 'updated_at'))
    except (IngestionJob.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Ingestion job not found.'}, status=404)
    return JsonResponse(_v2_job_payload(job))


@login_required
@require_POST
def cancel_ingestion_job_v2(request, job_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .models import IngestionJob

    try:
        job = IngestionJob.objects.select_related('document').get(pk=job_id)
    except (IngestionJob.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Ingestion job not found.'}, status=404)
    if job.status in (IngestionJob.Status.SUCCEEDED, IngestionJob.Status.CANCELLED):
        return JsonResponse({'error': f'Job cannot be cancelled from status {job.status}.'}, status=409)
    job.cancel_requested = True
    if job.status == IngestionJob.Status.PENDING:
        job.status = IngestionJob.Status.CANCELLED
    job.save(update_fields=('cancel_requested', 'status', 'updated_at'))
    return JsonResponse(_v2_job_payload(job))


@login_required
@require_POST
def archive_catalog_document_v2(request, document_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .models import CatalogDocument
    from .services.documents import archive_document

    try:
        document = CatalogDocument.objects.get(pk=document_id)
    except (CatalogDocument.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Catalog document not found.'}, status=404)
    archive_document(document)
    return JsonResponse({'message': 'Document archived.', 'document_id': str(document.id)})


@login_required
def index_audit_v2(request, document_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .models import CatalogDocument
    from .services.indexing import index_dry_run

    try:
        document = CatalogDocument.objects.get(pk=document_id)
    except (CatalogDocument.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Catalog document not found.'}, status=404)
    return JsonResponse(index_dry_run(document).as_dict())


@login_required
@require_POST
def execute_index_v2(request, document_id):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        confirmed = int(body.get('confirmed_embedding_count'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'error': 'confirmed_embedding_count is required.'}, status=400)

    from .models import CatalogDocument
    from .services.indexing import index_document

    try:
        document = CatalogDocument.objects.get(pk=document_id)
        result = index_document(document, confirmed_embedding_count=confirmed)
        
        # Update progress to 'indexed' (75%) after successful indexing
        pdf_progress = request.session.get('pdf_progress', {})
        approved_pdfs = request.session.get('approved_pdfs', [])
        # Update progress for any matching filename (stem or full name)
        for pdf_name in approved_pdfs:
            if Path(pdf_name).stem == document.original_filename or pdf_name == document.original_filename:
                pdf_progress[pdf_name] = 'indexed'
        request.session['pdf_progress'] = pdf_progress
        request.session.modified = True
        
        _invalidate_stats_cache()
        return JsonResponse(result)
    except CatalogDocument.DoesNotExist:
        return JsonResponse({'error': 'Catalog document not found.'}, status=404)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=409)
    except Exception as exc:
        import traceback, logging
        logging.error('execute_index_v2 error: %s', traceback.format_exc())
        return JsonResponse({'error': f'V2 indexing failed: {exc}', 'detail': traceback.format_exc()}, status=500)
