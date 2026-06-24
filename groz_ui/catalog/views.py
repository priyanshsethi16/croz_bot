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
    splits_dir = input_dir / 'splits'
    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    pdfs = []
    pdfs_with_splits = set()  # Track which PDFs have been split
    
    # First, check which PDFs have splits
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                split_files = list(stem_dir.glob('*.pdf'))
                if split_files:  # If this stem has split files
                    pdfs_with_splits.add(stem_dir.name)
                    pdfs.extend(split_files)
    
    # Get main PDFs (but exclude those that have been split)
    if input_dir.exists():
        for pdf in input_dir.glob('*.pdf'):
            if pdf.stem not in pdfs_with_splits:
                pdfs.append(pdf)
    
    processed = []
    processed_names = set()

    # Check database for processed PDFs
    try:
        from .models import CatalogDocument, DocumentChunk
        for doc in CatalogDocument.objects.exclude(status=CatalogDocument.Status.ARCHIVED).order_by('original_filename'):
            chunk_count = DocumentChunk.objects.filter(document=doc).count()
            if chunk_count > 0:
                # Check how many chunks are actually indexed to Qdrant
                indexed_chunks = DocumentChunk.objects.filter(
                    document=doc,
                    index_status=DocumentChunk.IndexStatus.INDEXED
                ).count()
                processed.append({
                    'name': doc.original_filename,
                    'chunks': chunk_count,
                    'indexed_chunks': indexed_chunks,
                })
                processed_names.add(doc.original_filename)
    except Exception:
        pass

    # Also check filesystem for processed PDFs (vision_pipeline/data)
    if data_dir.exists():
        for stem_dir in data_dir.iterdir():
            if not stem_dir.is_dir():
                continue
            assembled_file = stem_dir / 'assembled_products.json'
            if not assembled_file.exists():
                continue
            
            # Find matching PDF filename
            pdf_filename = stem_dir.name + '.pdf'
            if pdf_filename in processed_names:
                continue  # Already counted from database
            
            # Count products from filesystem
            try:
                import json as _json
                with open(assembled_file, 'r', encoding='utf-8') as f:
                    products = _json.load(f)
                    product_count = len(products) if isinstance(products, list) else 0
                    if product_count > 0:
                        processed.append({
                            'name': pdf_filename,
                            'chunks': product_count,
                            'indexed_chunks': 0,  # Filesystem PDFs not indexed yet
                        })
                        processed_names.add(pdf_filename)
            except Exception:
                pass

    try:
        from rag_pipeline.providers import indexed_document_count
        indexed = indexed_document_count()
    except Exception:
        indexed = 0

    unprocessed = [p.name for p in pdfs if p.name not in processed_names]

    stats = {
        'total_pdfs': len(pdfs),
        'processed': processed,
        'unprocessed': unprocessed,
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
            # Reset per-session workflow progress on every fresh login
            request.session['session_uploaded_pdfs'] = []
            request.session['session_chunks_created'] = 0
            request.session['session_indexed'] = 0
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

    # Check if file exists in main input directory
    dest = PROJECT_ROOT / 'input' / pdf.name
    if dest.exists():
        return JsonResponse({'error': f'"{pdf.name}" already exists. Delete it first or rename the file.'}, status=409)
    
    # Check if it exists as a split PDF
    stem = Path(pdf.name).stem
    splits_dir = PROJECT_ROOT / 'input' / 'splits'
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                # Check if any split file with this stem exists
                if any(stem_dir.glob(f'{stem}*.pdf')):
                    return JsonResponse({'error': f'PDF "{pdf.name}" has already been split. Delete split parts first.'}, status=409)
    
    # Check if already processed in database
    try:
        from .models import CatalogDocument
        if CatalogDocument.objects.filter(original_filename=pdf.name).exists():
            return JsonResponse({'error': f'"{pdf.name}" already exists in the database. Delete it first.'}, status=409)
    except Exception:
        pass
    
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as f:
        for chunk in pdf.chunks():
            f.write(chunk)

    # Track per-session uploads
    uploaded = request.session.get('session_uploaded_pdfs', [])
    if pdf.name not in uploaded:
        uploaded.append(pdf.name)
    request.session['session_uploaded_pdfs'] = uploaded
    request.session.modified = True

    return JsonResponse({'message': f'"{pdf.name}" uploaded successfully.', 'filename': pdf.name})


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

    # Check if it's a main PDF or a split PDF
    pdf_path = PROJECT_ROOT / 'input' / filename
    if not pdf_path.exists():
        # Check in splits directory
        splits_dir = PROJECT_ROOT / 'input' / 'splits'
        if splits_dir.exists():
            for stem_dir in splits_dir.iterdir():
                if stem_dir.is_dir():
                    possible_path = stem_dir / filename
                    if possible_path.exists():
                        pdf_path = possible_path
                        break
    
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

        # Auto-ingest products into database
        stem = pdf_path.stem
        products_file = PROJECT_ROOT / 'vision_pipeline' / 'data' / stem / 'assembled_products.json'
        if products_file.exists():
            try:
                from .models import CatalogDocument, Catalog
                from .services.structured_ingestion import persist_assembled_products
                
                # Get or create document
                doc, created = CatalogDocument.objects.get_or_create(
                    original_filename=filename,
                    defaults={
                        'uploaded_by': request.user,
                        'catalog': Catalog.objects.first(),  # Use first catalog or None
                        'source_type': CatalogDocument.SourceType.CATALOG,
                        'status': CatalogDocument.Status.UPLOADED,
                    }
                )
                
                # Load and persist products
                with open(products_file, 'r', encoding='utf-8') as f:
                    products = json.load(f)
                
                stats = persist_assembled_products(doc, products, replace=True)
                output += f"\n\n✓ Ingested {stats['families']} products with {stats['variants']} variants into database."
            except Exception as e:
                output += f"\n⚠ Warning: Could not ingest products into database: {e}"

        # Track per-session chunks created
        try:
            import re as _re
            match = _re.search(r'Products found\s*:\s*(\d+)', output)
            if match:
                request.session['session_chunks_created'] = int(match.group(1))
                request.session.modified = True
        except Exception:
            pass

        return JsonResponse({'message': 'Pipeline completed.', 'output': output[-3000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out (10 min limit).'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: List uploaded PDFs ──────────────────────────────────────────────────

@login_required
def list_pdfs(request):
    input_dir = PROJECT_ROOT / 'input'
    splits_dir = input_dir / 'splits'
    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    pdfs = []
    pdfs_with_splits = set()
    
    if splits_dir.exists():
        for stem_dir in splits_dir.iterdir():
            if stem_dir.is_dir():
                split_files = sorted(p.name for p in stem_dir.glob('*.pdf'))
                if split_files:
                    pdfs_with_splits.add(stem_dir.name)
                    pdfs.extend(split_files)
    
    if input_dir.exists():
        for pdf in sorted(input_dir.glob('*.pdf')):
            if pdf.stem not in pdfs_with_splits:
                pdfs.append(pdf.name)
    
    chunked = set()
    try:
        from .models import CatalogDocument, DocumentChunk
        for doc in CatalogDocument.objects.all():
            if DocumentChunk.objects.filter(document=doc).exists():
                chunked.add(doc.original_filename)
    except Exception:
        pass
    
    if data_dir.exists():
        for stem_dir in data_dir.iterdir():
            if not stem_dir.is_dir():
                continue
            assembled_file = stem_dir / 'assembled_products.json'
            if assembled_file.exists():
                pdf_filename = stem_dir.name + '.pdf'
                chunked.add(pdf_filename)
    
    return JsonResponse({'pdfs': pdfs, 'chunked': list(chunked)})


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
        filename = Path(filename).name
        pdf_path = PROJECT_ROOT / 'input' / filename
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
    # Override progress with per-session counters
    stats['session_uploaded_pdfs'] = request.session.get('session_uploaded_pdfs', [])
    stats['session_chunks'] = request.session.get('session_chunks_created', 0)
    stats['session_indexed'] = request.session.get('session_indexed', 0)
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
    pdf_path = PROJECT_ROOT / 'input' / filename
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

    pdf_path = PROJECT_ROOT / 'input' / filename
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
        
        # Auto-ingest products into database
        part_stem = pdf_path.stem
        products_file = PROJECT_ROOT / 'vision_pipeline' / 'data' / part_stem / 'assembled_products.json'
        if products_file.exists():
            try:
                from .models import CatalogDocument, Catalog
                from .services.structured_ingestion import persist_assembled_products
                
                # Get or create document for this split part
                doc, created = CatalogDocument.objects.get_or_create(
                    original_filename=part_file,
                    defaults={
                        'uploaded_by': request.user,
                        'catalog': Catalog.objects.first(),
                        'source_type': CatalogDocument.SourceType.CATALOG,
                        'status': CatalogDocument.Status.UPLOADED,
                    }
                )
                
                # Load and persist products
                with open(products_file, 'r', encoding='utf-8') as f:
                    products = json.load(f)
                
                stats = persist_assembled_products(doc, products, replace=True)
                output += f"\n\n✓ Ingested {stats['families']} products with {stats['variants']} variants into database."
            except Exception as e:
                output += f"\n⚠ Warning: Could not ingest products into database: {e}"
        
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

    pdf_path = PROJECT_ROOT / 'input' / filename
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
    stem     = Path(filename).stem
    pdf_path = PROJECT_ROOT / 'input' / filename

    # Delete main PDF if exists
    if pdf_path.exists():
        pdf_path.unlink()
    else:
        # Check if it's a split PDF
        splits_dir = PROJECT_ROOT / 'input' / 'splits'
        if splits_dir.exists():
            for stem_dir in splits_dir.iterdir():
                if stem_dir.is_dir():
                    split_path = stem_dir / filename
                    if split_path.exists():
                        split_path.unlink()
                        break

    # Delete splits directory for this stem (only if deleting main PDF)
    splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
    if splits_dir.exists():
        shutil.rmtree(splits_dir)

    # Delete data directory
    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / stem
    if data_dir.exists():
        shutil.rmtree(data_dir)

    # Delete from Qdrant
    try:
        from qdrant_client import models
        from rag_pipeline.providers import COLLECTION, build_qdrant_client
        client = build_qdrant_client()
        if client.collection_exists(COLLECTION):
            client.delete(
                collection_name=COLLECTION,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key='metadata.source_pdf',
                                match=models.MatchValue(value=stem),
                            )
                        ]
                    )
                ),
            )
    except Exception:
        pass

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

    chunks = []
    from .models import CatalogDocument, DocumentChunk
    
    # Try to find document by exact filename
    doc = CatalogDocument.objects.filter(
        original_filename=pdf_name,
        is_active=True
    ).order_by('-version').first()
    if not doc:
        doc = CatalogDocument.objects.filter(
            original_filename=pdf_name
        ).order_by('-version').first()

    if doc:
        db_chunks = DocumentChunk.objects.filter(document=doc).order_by('ordinal')
        for c in db_chunks:
            # Format chunk name as chunk_0000.md
            filename = f"chunk_{c.ordinal:04d}.md"
            chunks.append({
                'filename': filename,
                'content': c.text,
            })
    else:
        # If not found in DB, check filesystem for split PDFs
        stem = Path(pdf_name).stem
        data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / stem
        if data_dir.exists():
            assembled_file = data_dir / 'assembled_products.json'
            if assembled_file.exists():
                try:
                    with open(assembled_file, 'r', encoding='utf-8') as f:
                        products = json.load(f)
                    for idx, product in enumerate(products):
                        chunk_filename = f"chunk_{idx:04d}.md"
                        # Build markdown content from product data
                        content = f"# {product.get('product_name', 'Unknown Product')}\n\n"
                        if product.get('product_code'):
                            content += f"**Product Code:** `{product['product_code']}`\n\n"
                        if product.get('description'):
                            content += f"## Description\n{product['description']}\n\n"
                        if product.get('features'):
                            content += f"## Features\n"
                            for feat in product['features']:
                                content += f"- {feat}\n"
                            content += "\n"
                        chunks.append({
                            'filename': chunk_filename,
                            'content': content,
                        })
                except Exception:
                    pass

    return JsonResponse({'chunks': chunks})


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


@login_required
@require_POST
def index_pdf(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
        filename = body.get('filename', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)
    
    if not filename:
        return JsonResponse({'error': 'Missing filename.'}, status=400)
    
    try:
        from .models import CatalogDocument, DocumentChunk
        from rag_pipeline.providers import index_chunks_to_qdrant
        
        # Find the document
        doc = CatalogDocument.objects.filter(original_filename=filename).order_by('-version').first()
        if not doc:
            return JsonResponse({'error': f'Document "{filename}" not found in database.'}, status=404)
        
        # Get all chunks for this document
        chunks = DocumentChunk.objects.filter(document=doc).order_by('ordinal')
        if not chunks.exists():
            return JsonResponse({'error': f'No chunks found for "{filename}".'}, status=404)
        
        # Index chunks to Qdrant
        indexed_count = index_chunks_to_qdrant(doc, list(chunks))
        
        return JsonResponse({
            'message': f'Successfully indexed {indexed_count} chunks.',
            'indexed': indexed_count,
            'filename': filename
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


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
    if not settings.CATALOG_RAG_V2_INGEST:
        return JsonResponse({'error': 'Catalog RAG V2 ingestion is not enabled.'}, status=409)
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
        return JsonResponse(result)
    except CatalogDocument.DoesNotExist:
        return JsonResponse({'error': 'Catalog document not found.'}, status=404)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=409)
    except Exception as exc:
        return JsonResponse({'error': f'V2 indexing failed: {exc}'}, status=500)
