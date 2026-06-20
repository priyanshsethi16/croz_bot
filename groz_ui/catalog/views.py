import json
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


def _get_catalog_stats():
    """Return stats about processed PDFs and indexed chunks."""
    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    input_dir = PROJECT_ROOT / 'input'

    pdfs      = list(input_dir.glob('*.pdf')) if input_dir.exists() else []
    processed = []
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if d.is_dir():
                chunks = list((d / 'chunks').glob('*.md')) if (d / 'chunks').exists() else []
                prod_json = d / 'products.json'
                products = []
                if prod_json.exists():
                    try:
                        products = json.loads(prod_json.read_text())
                    except Exception:
                        pass
                if chunks:
                    processed.append({
                        'name': d.name,
                        'chunks': len(chunks),
                        'products': len(products),
                    })

    try:
        import chromadb
        from rag_pipeline.providers import CHROMA_DIR, COLLECTION
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        col = client.get_collection(COLLECTION)
        indexed = col.count()
    except Exception:
        indexed = 0

    return {
        'total_pdfs': len(pdfs),
        'processed': processed,
        'unprocessed': [p.name for p in pdfs if p.stem not in {d['name'] for d in processed}],
        'indexed': indexed,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

def admin_login(request):
    error = ''
    if request.method == 'POST':
        user = authenticate(request,
                            username=request.POST.get('username'),
                            password=request.POST.get('password'))
        if user and user.is_active and user.is_staff:
            login(request, user)
            panel = request.GET.get('next_panel', '')
            url = '/admin-panel/' + (f'?panel={panel}' if panel else '')
            return redirect(url)
        elif user and user.is_active and not user.is_staff:
            error = 'Access denied. This login is for administrators only.'
        else:
            error = 'Invalid credentials.'
    return render(request, 'catalog/login.html', {'error': error})


def admin_logout(request):
    logout(request)
    return redirect('/admin-panel/login/')


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    stats = _get_catalog_stats()
    return render(request, 'catalog/dashboard.html', {
        'stats': stats,
        'is_admin': request.user.is_staff,
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
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = PROJECT_ROOT / 'input' / filename
    if not pdf_path.exists():
        return JsonResponse({'error': f'File not found: {filename}'}, status=404)

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.gemini_api_key:
            return JsonResponse({'error': 'Gemini API key is not configured. Open Models & Keys.'}, status=400)
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600,
            env=subprocess_environment(),
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return JsonResponse({'error': output[-2000:]}, status=500)
        return JsonResponse({'message': 'Pipeline completed.', 'output': output[-3000:]})
    except subprocess.TimeoutExpired:
        return JsonResponse({'error': 'Pipeline timed out (10 min limit).'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: List uploaded PDFs ──────────────────────────────────────────────────

@login_required
def list_pdfs(request):
    input_dir = PROJECT_ROOT / 'input'
    data_dir  = PROJECT_ROOT / 'vision_pipeline' / 'data'
    pdfs = sorted(p.name for p in input_dir.glob('*.pdf')) if input_dir.exists() else []
    # Build set of stems that already have chunks
    chunked = set()
    if data_dir.exists():
        for d in data_dir.iterdir():
            if d.is_dir() and (d / 'chunks').exists() and list((d / 'chunks').glob('*.md')):
                chunked.add(d.name)
    return JsonResponse({'pdfs': pdfs, 'chunked': list(chunked)})


# ── API: PDF Preview (page thumbnails) ───────────────────────────────────

@login_required
def pdf_preview(request):
    import base64, fitz
    filename = request.GET.get('pdf', '').strip()
    if not filename:
        return JsonResponse({'error': 'No PDF specified.'}, status=400)
    pdf_path = PROJECT_ROOT / 'input' / Path(filename).name
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


# ── API: Run Ingest (indexing) ────────────────────────────────────────────────

@login_required
@require_POST
def run_ingest(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body  = json.loads(request.body)
        reset = body.get('reset', False)
    except Exception:
        reset = False

    cmd = [sys.executable, '-m', 'rag_pipeline.ingest']
    if reset:
        cmd.append('--reset')

    try:
        from .model_config import get_runtime_config, subprocess_environment
        runtime = get_runtime_config()
        if not runtime.openai_api_key:
            return JsonResponse({'error': 'OpenAI API key is required for embeddings. Open Models & Keys.'}, status=400)
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=300,
            env=subprocess_environment(),
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return JsonResponse({'error': output[-2000:]}, status=500)
        return JsonResponse({'message': 'Indexing completed.', 'output': output[-2000:]})
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
        from rag_pipeline.retriever import Retriever
        from rag_pipeline.llm import LLMAnswerer

        runtime = get_runtime_config()
        if not runtime.openai_api_key:
            return JsonResponse({'error': 'OpenAI API key is required for retrieval embeddings.'}, status=400)
        if not runtime.chat_api_key:
            return JsonResponse({'error': f'{runtime.chat_provider.title()} API key is not configured.'}, status=400)
        retriever = Retriever(top_k=5, api_key=runtime.openai_api_key)
        llm = LLMAnswerer(
            provider=runtime.chat_provider,
            api_key=runtime.chat_api_key,
            model=runtime.chat_model,
        )
        chunks    = retriever.retrieve(query)
        answer    = llm.answer(query, chunks)
        sources   = [
            {'name': c['metadata'].get('product_name', ''),
             'code': c['metadata'].get('product_code', ''),
             'score': c['score']}
            for c in chunks
        ]
        return JsonResponse({'answer': answer, 'sources': sources})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ── API: Catalog Stats (for dashboard refresh) ────────────────────────────────

@login_required
def catalog_stats(request):
    return JsonResponse(_get_catalog_stats())


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
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600,
            env=subprocess_environment(),
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return JsonResponse({'error': output[-2000:]}, status=500)
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

    if pdf_path.exists():
        pdf_path.unlink()

    splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
    if splits_dir.exists():
        shutil.rmtree(splits_dir)

    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / stem
    if data_dir.exists():
        shutil.rmtree(data_dir)

    try:
        import chromadb
        from rag_pipeline.providers import CHROMA_DIR, COLLECTION
        client  = chromadb.PersistentClient(path=CHROMA_DIR)
        col     = client.get_collection(COLLECTION)
        results = col.get(where={'source_pdf': stem})
        if results['ids']:
            col.delete(ids=results['ids'])
    except Exception:
        pass

    return JsonResponse({'message': f'"{filename}" and all associated data deleted.'})


# -- API: List chunks for a PDF ────────────────────────────────────────────────

@login_required
def list_chunks(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    pdf_name = request.GET.get('pdf', '').strip()
    if not pdf_name:
        return JsonResponse({'error': 'Missing pdf parameter.'}, status=400)

    chunks_dir = PROJECT_ROOT / 'vision_pipeline' / 'data' / pdf_name / 'chunks'
    if not chunks_dir.exists():
        return JsonResponse({'chunks': []})

    chunks = []
    for f in sorted(chunks_dir.glob('*.md')):
        chunks.append({
            'filename': f.name,
            'content': f.read_text(encoding='utf-8'),
        })
    return JsonResponse({'chunks': chunks})


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
