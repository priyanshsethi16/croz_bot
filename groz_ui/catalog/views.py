import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


PROJECT_ROOT = settings.PROJECT_ROOT


def _clean_parsing_instructions(value) -> str:
    """Limit optional admin PDF-specific VLM instructions before subprocess use."""
    return str(value or '').strip()[:4000]


def _get_catalog_stats():
    """Return stats about processed PDFs and indexed chunks."""
    input_dir = PROJECT_ROOT / 'input'
    splits_dir = PROJECT_ROOT / 'input' / 'splits'
    data_dir  = PROJECT_ROOT / 'vision_pipeline' / 'data'

    pdfs      = list(input_dir.glob('*.pdf')) if input_dir.exists() else []
    processed = []

    # 1. Add database-driven processed documents (from HEAD / dev)
    try:
        from .models import CatalogDocument, DocumentChunk
        for doc in CatalogDocument.objects.exclude(status=CatalogDocument.Status.ARCHIVED).order_by('original_filename'):
            chunk_count = DocumentChunk.objects.filter(document=doc).count()
            if chunk_count > 0:
                processed.append({
                    'name': doc.original_filename,
                    'chunks': chunk_count,
                    'products': doc.product_families.count(),
                })
    except Exception:
        pass

    # 2. Add filesystem-driven processed split parts / parent PDFs (from priyansh3)
    import re
    folder_to_pdf = {}
    
    # Map sanitized folder names to actual split PDF filenames
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                for split_pdf in stem_dir.glob('*.pdf'):
                    sanitized = re.sub(r'[^a-zA-Z0-9_\-]', '_', split_pdf.stem)[:60].strip('_')
                    folder_to_pdf[sanitized] = split_pdf.name
    
    # Scan splits_dir to find processed split parts
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                for split_pdf in stem_dir.glob('*.pdf'):
                    split_name = split_pdf.name
                    # Skip if already added
                    if any(p['name'] == split_name or p['name'].replace('.pdf', '') == split_name.replace('.pdf', '') for p in processed):
                        continue
                    sanitized_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', split_pdf.stem)[:60].strip('_')
                    data_folder = data_dir / sanitized_name if data_dir.exists() else None
                    chunks = []
                    products = []
                    if data_folder and data_folder.exists():
                        chunks = list((data_folder / 'chunks').glob('*.md')) if (data_folder / 'chunks').exists() else []
                        prod_json = data_folder / 'products.json'
                        if prod_json.exists():
                            try:
                                products = json.loads(prod_json.read_text())
                            except Exception:
                                pass
                    chunks_count = len(chunks) if chunks else len(products)
                    if chunks_count > 0:
                        processed.append({
                            'name': split_name,
                            'chunks': chunks_count,
                            'products': len(products),
                        })
    
    # Scan data_dir to find processed parent PDFs / other folders
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if d.is_dir():
                # Skip if already added
                if any(p['name'] == d.name or p['name'].replace('.pdf', '') == d.name or p['name'] == folder_to_pdf.get(d.name, '') for p in processed):
                    continue
                chunks = list((d / 'chunks').glob('*.md')) if (d / 'chunks').exists() else []
                prod_json = d / 'products.json'
                products = []
                if prod_json.exists():
                    try:
                        products = json.loads(prod_json.read_text())
                    except Exception:
                        pass
                chunks_count = len(chunks) if chunks else len(products)
                if chunks_count > 0:
                    pdf_name = folder_to_pdf.get(d.name, d.name + '.pdf')
                    processed.append({
                        'name': pdf_name,
                        'chunks': chunks_count,
                        'products': len(products),
                    })

    try:
        from rag_pipeline.providers import indexed_document_count
        indexed = indexed_document_count()
    except Exception:
        indexed = 0

    stats = {
        'total_pdfs': len(pdfs),
        'processed': processed,
        'unprocessed': [p.name for p in pdfs if not any(
            proc['name'] == p.name or proc['name'] == p.stem
            for proc in processed
        )],
        'indexed': indexed,
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
            'needs_review': ProductFamily.objects.filter(
                review_status=ProductFamily.ReviewStatus.NEEDS_REVIEW,
            ).count(),
            'indexed_chunks': DocumentChunk.objects.filter(
                index_status=DocumentChunk.IndexStatus.INDEXED,
            ).count(),
        }
    except Exception:
        stats['v2'] = {}
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
    request.session.modified = True
    logout(request)
    return redirect('/')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    stats = _get_catalog_stats()
    return render(request, 'catalog/dashboard.html', {
        'stats': stats,
        'is_admin': request.user.is_staff,
        'v2_ingest_enabled': settings.CATALOG_RAG_V2_INGEST,
        'admin_access_token': request.session.get('admin_access_token', ''),
    })


# ── API: Upload PDF ───────────────────────────────────────────────────────────

@login_required
@require_POST
def upload_pdf(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    pdf = request.FILES.get('pdf')
    if not pdf or not pdf.name.endswith('.pdf'):
        return JsonResponse({'error': 'Please upload a valid PDF file.'}, status=400)

    dest = PROJECT_ROOT / 'input' / pdf.name
    if dest.exists():
        return JsonResponse({'error': f'"{pdf.name}" already exists. Delete it first or rename the file.'}, status=409)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as f:
        for chunk in pdf.chunks():
            f.write(chunk)

    # Don't auto-track uploaded PDFs - only track when user approves after preview
    return JsonResponse({'message': f'"{pdf.name}" uploaded successfully.', 'filename': pdf.name})


def _resolve_pdf_path(filename: str) -> Path:
    """Find a PDF file in input/ or recursively inside input/splits/ subfolders."""
    filename = Path(filename).name
    pdf_path = PROJECT_ROOT / 'input' / filename
    if not pdf_path.exists():
        splits_dir = PROJECT_ROOT / 'input' / 'splits'
        if splits_dir.exists():
            for stem_dir in splits_dir.iterdir():
                if stem_dir.is_dir():
                    candidate = stem_dir / filename
                    if candidate.exists():
                        pdf_path = candidate
                        break
    return pdf_path


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
    
    # Temporarily set is_active = False so we can update / replace the products
    was_active = doc.is_active
    if was_active:
        doc.is_active = False
        doc.save(update_fields=['is_active'])
        
    try:
        # Use transaction to ensure consistency
        with transaction.atomic():
            from .services.structured_ingestion import persist_assembled_products
            persist_assembled_products(doc, assembled, replace=True)
    finally:
        # Restore is_active state
        doc.is_active = True
        doc.status = doc.Status.READY
        doc.save(update_fields=['is_active', 'status'])


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
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600,
            env=env,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return JsonResponse({'error': output[-2000:]}, status=500)

        # Update progress for this specific PDF to 'chunked' (50%)
        pdf_progress = request.session.get('pdf_progress', {})
        approved_pdfs = request.session.get('approved_pdfs', [])
        if filename in approved_pdfs:
            pdf_progress[filename] = 'chunked'
            request.session['pdf_progress'] = pdf_progress
            request.session.modified = True

        # Auto-ingest into the database
        try:
            import re as _re
            _stem = pdf_path.stem
            _sanitized = _re.sub(r'[^a-zA-Z0-9_\-]', '_', _stem)[:60].strip('_') or 'catalog'
            _data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / _sanitized
            _assembled_file = _data_dir / 'assembled_products.json'
            _products_file = _data_dir / 'products.json'

            _ingest_file = _assembled_file if _assembled_file.exists() else (_products_file if _products_file.exists() else None)
            if _ingest_file:
                _doc = _ensure_catalog_document(pdf_path)
                _ingest_data = json.loads(_ingest_file.read_text(encoding='utf-8'))
                if isinstance(_ingest_data, list):
                    _ingest_assembled_products_for_document(_doc, _ingest_data)
        except Exception as _ingest_err:
            import logging
            logging.warning(f"Auto-ingestion failed in run_pipeline: {_ingest_err}", exc_info=True)

        return JsonResponse({'message': 'Pipeline completed.', 'output': output[-3000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out (10 min limit).'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: List uploaded PDFs ──────────────────────────────────────────────────

@login_required
def list_pdfs(request):
    input_dir = PROJECT_ROOT / 'input'
    splits_dir = PROJECT_ROOT / 'input' / 'splits'
    data_dir  = PROJECT_ROOT / 'vision_pipeline' / 'data'
    
    pdfs = sorted(p.name for p in input_dir.glob('*.pdf')) if input_dir.exists() else []
    
    # Track which parent PDFs have been split
    split_parent_stems = set()
    
    # Add split PDFs from input/splits/
    split_pdfs = []
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                # Check if this directory has any split PDFs
                split_files = list(stem_dir.glob('*.pdf'))
                if split_files:
                    # Mark this parent as having splits
                    split_parent_stems.add(stem_dir.name)
                    # Add all split PDFs from this directory
                    for split_pdf in sorted(split_files):
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
    
    # Build set of stems that already have chunks
    chunked = set()
    try:
        from .models import CatalogDocument, DocumentChunk
        for doc in CatalogDocument.objects.all():
            if DocumentChunk.objects.filter(document=doc).exists():
                chunked.add(doc.original_filename)
    except Exception:
        pass

    if data_dir.exists():
        for d in data_dir.iterdir():
            if d.is_dir():
                has_chunks = (d / 'chunks').exists() and list((d / 'chunks').glob('*.md'))
                has_products = (d / 'products.json').exists()
                if has_chunks or has_products:
                    chunked.add(d.name)

    # Gather rich metadata for each PDF/part
    pdf_details = []
    import re
    try:
        from .models import CatalogDocument, DocumentChunk, IngestionJob
        for pdf_name in all_pdfs:
            pdf_path = _resolve_pdf_path(pdf_name)
            stem = pdf_path.stem
            
            doc = None
            if pdf_path.exists():
                try:
                    import hashlib
                    checksum = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                    doc = CatalogDocument.objects.filter(checksum_sha256=checksum).first()
                except Exception:
                    pass
            
            if not doc:
                doc = CatalogDocument.objects.filter(original_filename=stem).order_by('-version').first()
            if not doc:
                doc = CatalogDocument.objects.filter(original_filename=pdf_name).order_by('-version').first()
                
            chunks_count = 0
            status = 'Pending'
            doc_id = None
            has_embeddings = False
            
            if doc:
                doc_id = str(doc.id)
                chunks_count = DocumentChunk.objects.filter(document=doc).count()
                
                # Check for running ingestion job
                running_job = IngestionJob.objects.filter(
                    document=doc,
                    status__in=[IngestionJob.Status.PENDING, IngestionJob.Status.RUNNING]
                ).first()
                
                if running_job:
                    status = 'Processing'
                else:
                    if doc.status == CatalogDocument.Status.UPLOADED:
                        status = 'Pending'
                    elif doc.status == CatalogDocument.Status.EXTRACTING:
                        status = 'Processing'
                    elif doc.status == CatalogDocument.Status.INDEXING:
                        status = 'Processing'
                    elif doc.status == CatalogDocument.Status.READY:
                        status = 'Ready'
                    elif doc.status == CatalogDocument.Status.REVIEW:
                        status = 'Ready'
                    elif doc.status == CatalogDocument.Status.FAILED:
                        status = 'Failed'
                    else:
                        status = doc.status
                
                has_embeddings = (doc.status == CatalogDocument.Status.READY and doc.is_active)
                if not has_embeddings:
                    has_embeddings = DocumentChunk.objects.filter(document=doc, index_status=DocumentChunk.IndexStatus.INDEXED).exists()
            else:
                # Check filesystem chunks
                sanitized_stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', stem)[:60].strip('_')
                chunks_dir = data_dir / sanitized_stem / 'chunks'
                if chunks_dir.exists():
                    chunks_count = len(list(chunks_dir.glob('*.md')))
                    if chunks_count > 0:
                        status = 'Ready'
            
            pdf_details.append({
                'name': pdf_name,
                'chunks_count': chunks_count,
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
        'v2_ingest_enabled': settings.CATALOG_RAG_V2_INGEST
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

    STAGE_ORDER = ['uploaded', 'chunked', 'indexed', 'tested']
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
            # Check DB stage map for this part
            part_stage = _stage_map.get(part_name) or _stage_map.get(part_stem)
            if part_stage in STAGE_ORDER:
                part_stages.append(part_stage)
            else:
                # Check DB chunks
                try:
                    doc = CatalogDocument.objects.filter(
                        original_filename__in=[part_stem, part_name]
                    ).order_by('-version').first()
                    if doc:
                        indexed_count = DocumentChunk.objects.filter(
                            document=doc, index_status=DocumentChunk.IndexStatus.INDEXED
                        ).count()
                        chunks_count = DocumentChunk.objects.filter(document=doc).count()
                        if indexed_count > 0:
                            part_stages.append('indexed')
                        elif chunks_count > 0:
                            part_stages.append('chunked')
                        else:
                            part_stages.append('uploaded')
                    else:
                        # Fallback: check filesystem vision_pipeline/data
                        import re as _re
                        sanitized = _re.sub(r'[^a-zA-Z0-9_\-]', '_', part_stem)[:60].strip('_')
                        data_dir = Path(__file__).resolve().parent.parent.parent / 'vision_pipeline' / 'data'
                        chunks_dir = data_dir / sanitized / 'chunks'
                        if chunks_dir.exists() and list(chunks_dir.glob('*.md')):
                            part_stages.append('chunked')
                        elif (data_dir / sanitized / 'products.json').exists():
                            part_stages.append('chunked')
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
            doc = CatalogDocument.objects.filter(
                original_filename__in=[pdf_stem, filename]
            ).order_by('-version').first()

            if doc:
                chunks_count = DocumentChunk.objects.filter(document=doc).count()
                indexed_count = DocumentChunk.objects.filter(
                    document=doc,
                    index_status=DocumentChunk.IndexStatus.INDEXED
                ).count()
                if indexed_count > 0:
                    detected_stage = 'indexed'
                elif chunks_count > 0:
                    detected_stage = 'chunked'
        except Exception:
            pass

    # Load persisted stage from DB (survives logout/login)
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
    except Exception:
        _stage_map = {}
    db_stage = _stage_map.get(filename)
    if db_stage not in STAGE_ORDER:
        db_stage = None

    # Never downgrade only if detected_stage confirms data still exists (> uploaded means chunks/index present)
    # If detected_stage is 'uploaded' (no chunks/index found), trust it — data was deleted
    if detected_stage == 'uploaded':
        final_stage = 'uploaded'
    else:
        existing_session_stage = request.session.get('pdf_progress', {}).get(filename)
        candidate_stages = [s for s in [detected_stage, existing_session_stage, db_stage] if s in STAGE_ORDER]
        final_stage = max(candidate_stages, key=lambda s: STAGE_ORDER.index(s))

    # Replace — only track one PDF at a time
    request.session['approved_pdfs'] = [filename]
    request.session['pdf_progress'] = {filename: final_stage}
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
    """Update a PDF's progress stage: uploaded (25%), chunked (50%), indexed (75%), tested (100%)."""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        filename = body.get('filename', '').strip()
        stage = body.get('stage', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    
    if not filename or stage not in ['uploaded', 'chunked', 'indexed', 'tested']:
        return JsonResponse({'error': 'Invalid filename or stage.'}, status=400)

    STAGE_ORDER = ['uploaded', 'chunked', 'indexed', 'tested']

    # Persist to DB (ApiKey table) so stage survives logout/login
    from .models import ApiKey
    import json as _j
    try:
        _stage_map = _j.loads(ApiKey.objects.get(name='pdf_stage_map').value)
    except Exception:
        _stage_map = {}
    current_db_stage = _stage_map.get(filename, 'uploaded')
    if current_db_stage not in STAGE_ORDER:
        current_db_stage = 'uploaded'

    # Never downgrade
    if STAGE_ORDER.index(stage) < STAGE_ORDER.index(current_db_stage):
        stage = current_db_stage

    _stage_map[filename] = stage
    ApiKey.objects.update_or_create(name='pdf_stage_map', defaults={'value': _j.dumps(_stage_map)})

    pdf_progress = request.session.get('pdf_progress', {})
    pdf_progress[filename] = stage
    request.session['pdf_progress'] = pdf_progress
    approved_pdfs = request.session.get('approved_pdfs', [])
    if filename not in approved_pdfs:
        approved_pdfs = [filename]
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
    if part_file:
        if not stem:
            return JsonResponse({'error': 'Missing split stem.'}, status=400)
        filename = Path(part_file).name
        pdf_path = PROJECT_ROOT / 'input' / 'splits' / Path(stem).name / filename
    else:
        if not filename:
            return JsonResponse({'error': 'No PDF specified.'}, status=400)
        pdf_path = _resolve_pdf_path(filename)
                            
    if not pdf_path.exists():
        return JsonResponse({'error': 'PDF not found.'}, status=404)
    try:
        doc   = fitz.open(str(pdf_path))
        total = len(doc)
        pages = []
        for i in range(min(total, 50)):
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
        return JsonResponse({'pages': pages, 'total': total})
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

        runtime = get_runtime_config()
        if not runtime.openai_api_key:
            return JsonResponse({'error': 'OPENAI_API_KEY is required for retrieval embeddings.'}, status=400)
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
        )
        payload = execution.as_dict()
        payload['query_path'] = 'v2'
        return JsonResponse(payload)
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

    # Restore from DB for any tracked PDF whose session state is missing/outdated
    from .models import ApiKey
    STAGE_ORDER = ['uploaded', 'chunked', 'indexed', 'tested']
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
                tracked_stage = pdf_progress.get(fname)
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
                    vision_data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
                    total_parts = len(split_part_files)
                    chunked_count = 0
                    indexed_count = 0

                    for p in split_part_files:
                        part_stem = Path(p).stem
                        has_chunks = False
                        has_index  = False

                        # --- Check chunks ---
                        # 1. session/DB stage map
                        if (pdf_progress.get(p) or db_map.get(p) or
                                pdf_progress.get(part_stem) or db_map.get(part_stem)) in STAGE_ORDER[1:]:
                            has_chunks = True
                        # 2. DB DocumentChunk records
                        if not has_chunks:
                            try:
                                doc = CatalogDocument.objects.filter(
                                    original_filename__in=[part_stem, p]
                                ).first()
                                if doc and DocumentChunk.objects.filter(document=doc).exists():
                                    has_chunks = True
                            except Exception:
                                pass
                        # 3. Filesystem vision_pipeline/data
                        if not has_chunks:
                            sanitized = _re2.sub(r'[^a-zA-Z0-9_\-]', '_', part_stem)[:60].strip('_')
                            data_folder = vision_data_dir / sanitized
                            if data_folder.exists() and (
                                list(data_folder.glob('chunks/*.md')) or
                                (data_folder / 'products.json').exists()
                            ):
                                has_chunks = True

                        if has_chunks:
                            chunked_count += 1

                        # --- Check indexed ---
                        # 1. session/DB stage map
                        part_stage = (pdf_progress.get(p) or db_map.get(p) or
                                      pdf_progress.get(part_stem) or db_map.get(part_stem))
                        if part_stage in ('indexed', 'tested'):
                            has_index = True
                        # 2. DB indexed chunks
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
                        tracked_stage = 'indexed'   # 75%
                    elif indexed_count > 0 or chunked_count == total_parts:
                        # Between 50% and 75%: all chunked + some indexed
                        tracked_percent = 50 + (indexed_count / total_parts) * 25
                        tracked_stage = 'chunked'
                    elif chunked_count > 0:
                        # Between 25% and 50%: some chunked, none indexed
                        tracked_percent = 25 + (chunked_count / total_parts) * 25
                        tracked_stage = 'uploaded'
                    else:
                        tracked_stage = 'uploaded'  # 25%

                except Exception:
                    pass

    if tracked_pdf and tracked_stage:
        stats['tracked_pdf']   = tracked_pdf
        stats['tracked_stage'] = tracked_stage
        if tracked_percent is not None:
            stats['tracked_percent']   = round(tracked_percent, 2)
            stats['total_split_parts'] = total_parts
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
    pdf_path = _resolve_pdf_path(filename)
    if not pdf_path.exists():
        return JsonResponse({'error': 'File not found.'}, status=404)

    cache_key = f"{filename}:{pdf_path.stat().st_mtime}"
    if cache_key in _page_count_cache:
        return JsonResponse({'pages': _page_count_cache[cache_key], 'filename': filename})

    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(pdf_path)).pages)
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

    pdf_path = _resolve_pdf_path(filename)
    if not pdf_path.exists():
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from pypdf import PdfReader, PdfWriter
        reader   = PdfReader(str(pdf_path))
        total    = len(reader.pages)
        stem     = pdf_path.stem
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
        env = subprocess_environment()
        if parsing_instructions:
            env['PDF_PARSING_INSTRUCTIONS'] = parsing_instructions
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600,
            env=env,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return JsonResponse({'error': output[-2000:]}, status=500)

        # Auto-ingest into the database
        try:
            import re as _re
            _stem = pdf_path.stem
            _sanitized = _re.sub(r'[^a-zA-Z0-9_\-]', '_', _stem)[:60].strip('_') or 'catalog'
            _data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / _sanitized
            _assembled_file = _data_dir / 'assembled_products.json'
            _products_file = _data_dir / 'products.json'

            _ingest_file = _assembled_file if _assembled_file.exists() else (_products_file if _products_file.exists() else None)
            if _ingest_file:
                _doc = _ensure_catalog_document(pdf_path)
                _ingest_data = json.loads(_ingest_file.read_text(encoding='utf-8'))
                if isinstance(_ingest_data, list):
                    _ingest_assembled_products_for_document(_doc, _ingest_data)
        except Exception as _ingest_err:
            import logging
            logging.warning(f"Auto-ingestion failed in run_pipeline_split: {_ingest_err}", exc_info=True)

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

    pdf_path = _resolve_pdf_path(filename)
    if not pdf_path.exists():
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from pypdf import PdfReader, PdfWriter
        reader  = PdfReader(str(pdf_path))
        total   = len(reader.pages)
        stem    = pdf_path.stem
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
        # Extract parent stem (remove the _pXXXX-XXXX or _custom_pXXXX-XXXX part)
        parent_stem = re.sub(r'_(custom_)?p\d{4}-\d{4}$', '', stem)
        
        # Delete the specific split PDF file from input/splits/ParentName/ directory
        splits_parent_dir = PROJECT_ROOT / 'input' / 'splits' / parent_stem
        split_pdf_path = splits_parent_dir / filename
        if split_pdf_path.exists():
            split_pdf_path.unlink()
            print(f'Deleted split PDF: {split_pdf_path}')
        
        # Check if this was the last split in the directory
        if splits_parent_dir.exists():
            remaining_splits = list(splits_parent_dir.glob('*.pdf'))
            if not remaining_splits:
                # No more splits, delete the parent directory and the parent PDF
                shutil.rmtree(splits_parent_dir)
                parent_pdf = PROJECT_ROOT / 'input' / f'{parent_stem}.pdf'
                if parent_pdf.exists():
                    parent_pdf.unlink()
                    print(f'Deleted parent PDF: {parent_pdf}')
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
            # Try both sanitized and original stem
            for source_stem in [stem, sanitized_stem]:
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
    try:
        from .models import CatalogDocument
        from .services.documents import archive_document
        
        # Find and delete documents matching filename
        docs_to_delete = CatalogDocument.objects.filter(original_filename=filename)
        for doc in docs_to_delete:
            try:
                archive_document(doc)
            except Exception:
                pass
            try:
                doc.delete()
            except Exception:
                pass
        
        # Also find split parts starting with this stem
        split_docs = CatalogDocument.objects.filter(
            original_filename__startswith=stem + '_'
        )
        for doc in split_docs:
            try:
                archive_document(doc)
            except Exception:
                pass
            try:
                doc.delete()
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

    return JsonResponse({'message': f'"{filename}" and all associated data deleted.'})


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
    doc = CatalogDocument.objects.filter(original_filename=pdf_stem).order_by('-version').first()
    if not doc:
        doc = CatalogDocument.objects.filter(original_filename=pdf_name).order_by('-version').first()

    document_id = str(doc.id) if doc else None

    if doc:
        db_chunks = DocumentChunk.objects.filter(document=doc).order_by('ordinal')
        for c in db_chunks:
            prod_name = 'General Info'
            if c.family:
                prod_name = c.family.product_name
            elif c.variant and c.variant.family:
                prod_name = c.variant.family.product_name
            chunks.append({
                'filename': f"chunk_{c.ordinal:04d}.md",
                'content': c.text,
                'ordinal': c.ordinal,
                'product_name': prod_name,
                'status': 'Embedded' if c.index_status == 'indexed' else 'Ready',
            })
    else:
        # No direct doc — check if this is a parent stem with split part documents
        splits_dir = PROJECT_ROOT / 'input' / 'splits' / pdf_stem
        if splits_dir.exists():
            split_stems = sorted(
                re.sub(r'[^a-zA-Z0-9_\-]', '_', p.stem)[:60].strip('_')
                for p in splits_dir.glob('*.pdf')
            )
            split_docs = CatalogDocument.objects.filter(
                original_filename__in=split_stems
            ).order_by('original_filename')
            if split_docs.exists():
                # Use first split doc's id for chat context (covers all splits)
                document_id = str(split_docs.first().id)
                ordinal_offset = 0
                for split_doc in split_docs:
                    db_chunks = DocumentChunk.objects.filter(document=split_doc).order_by('ordinal')
                    for c in db_chunks:
                        prod_name = 'General Info'
                        if c.family:
                            prod_name = c.family.product_name
                        elif c.variant and c.variant.family:
                            prod_name = c.variant.family.product_name
                        chunks.append({
                            'filename': f"chunk_{ordinal_offset + c.ordinal:04d}.md",
                            'content': c.text,
                            'ordinal': ordinal_offset + c.ordinal,
                            'product_name': prod_name,
                            'status': 'Embedded' if c.index_status == 'indexed' else 'Ready',
                        })
                    ordinal_offset += DocumentChunk.objects.filter(document=split_doc).count()

    return JsonResponse({'document_id': document_id, 'chunks': chunks})


@login_required
@require_POST
def save_chunk(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        pdf_name = Path(str(body.get('pdf', '')).strip()).name
        chunk_name = Path(str(body.get('filename', '')).strip()).name
        content = str(body.get('content', ''))
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not pdf_name or not chunk_name.endswith('.md'):
        return JsonResponse({'error': 'Invalid chunk file.'}, status=400)

    # Save to the database directly
    from .models import CatalogDocument, DocumentChunk
    import hashlib
    
    ordinal = _parse_ordinal_from_filename(chunk_name)
    db_updated = False
    
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
    if not settings.CATALOG_RAG_V2_INGEST:
        return JsonResponse({'error': 'Catalog RAG V2 ingestion is not enabled.'}, status=409)

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
    if not settings.CATALOG_RAG_V2_INGEST:
        return JsonResponse({'error': 'Catalog RAG V2 ingestion is not enabled.'}, status=409)

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
        result = index_document(document, confirmed_embedding_count=confirmed, allow_disabled=True)
        
        # Update progress to 'indexed' (75%) after successful indexing
        pdf_progress = request.session.get('pdf_progress', {})
        approved_pdfs = request.session.get('approved_pdfs', [])
        # Update progress for any matching filename (stem or full name)
        for pdf_name in approved_pdfs:
            if Path(pdf_name).stem == document.original_filename or pdf_name == document.original_filename:
                pdf_progress[pdf_name] = 'indexed'
        request.session['pdf_progress'] = pdf_progress
        request.session.modified = True
        
        return JsonResponse(result)
    except CatalogDocument.DoesNotExist:
        return JsonResponse({'error': 'Catalog document not found.'}, status=404)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=409)
    except Exception as exc:
        return JsonResponse({'error': f'V2 indexing failed: {exc}'}, status=500)
