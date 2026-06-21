import json
import os
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


def _get_pdf_stem(folder_name: str, pdf_stems: set) -> str:
    """Map a data folder name back to its source PDF stem."""
    if folder_name in pdf_stems:
        return folder_name
    for stem in sorted(pdf_stems, key=len, reverse=True):
        # Match: stem_custom_p..., stem_p..., or stem_ (any suffix)
        if (folder_name.startswith(stem + '_') or
            folder_name.startswith(stem + 'p')):
            return stem
    return folder_name


def _get_catalog_stats():
    """Return stats about processed PDFs and indexed chunks."""
    data_dir  = PROJECT_ROOT / 'vision_pipeline' / 'data'
    input_dir = PROJECT_ROOT / 'input'

    pdfs      = list(input_dir.glob('*.pdf')) if input_dir.exists() else []
    pdf_stems = {p.stem for p in pdfs}

    # Aggregate chunks/products by parent PDF stem (groups split parts together)
    aggregated: dict[str, dict] = {}
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if not d.is_dir():
                continue
            chunks = list((d / 'chunks').glob('*.md')) if (d / 'chunks').exists() else []
            if not chunks:
                continue
            prod_json = d / 'products.json'
            products  = []
            if prod_json.exists():
                try:
                    products = json.loads(prod_json.read_text())
                except Exception:
                    pass
            parent = _get_pdf_stem(d.name, pdf_stems)
            if parent not in aggregated:
                aggregated[parent] = {'name': parent, 'chunks': 0, 'products': 0}
            aggregated[parent]['chunks']   += len(chunks)
            aggregated[parent]['products'] += len(products)

    processed = list(aggregated.values())

    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(PROJECT_ROOT / 'rag_pipeline' / 'chroma_db'))
        col    = client.get_collection('catalog_products')
        indexed = col.count()
    except Exception:
        indexed = 0

    processed_stems = {d['name'] for d in processed}
    return {
        'total_pdfs':   len(pdfs),
        'processed':    processed,
        'unprocessed':  [p.name for p in pdfs if p.stem not in processed_stems],
        'indexed':      indexed,
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
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600
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
    pdfs      = sorted(p.name for p in input_dir.glob('*.pdf')) if input_dir.exists() else []
    pdf_stems = {Path(p).stem for p in pdfs}

    chunked = set()
    if data_dir.exists():
        for d in data_dir.iterdir():
            if d.is_dir() and (d / 'chunks').exists() and list((d / 'chunks').glob('*.md')):
                parent = _get_pdf_stem(d.name, pdf_stems)
                chunked.add(parent)
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
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=300
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
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        from rag_pipeline.retriever import HybridRetriever
        from rag_pipeline.llm import LLMAnswerer

        retriever = HybridRetriever(top_k=5)
        llm       = LLMAnswerer(os.getenv('GROQ_API_KEY'))
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
    stats = _get_catalog_stats()
    # Compute total chunks from data folders (not chroma) for accurate display
    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    total_chunks = 0
    if data_dir.exists():
        for d in data_dir.iterdir():
            if d.is_dir() and (d / 'chunks').exists():
                total_chunks += len(list((d / 'chunks').glob('*.md')))
    stats['total_chunks'] = total_chunks
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
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    pdf_path = PROJECT_ROOT / 'input' / 'splits' / stem / part_file
    if not pdf_path.exists():
        return JsonResponse({'error': f'Split file not found: {part_file}'}, status=404)

    try:
        result = subprocess.run(
            [sys.executable, '-m', 'vision_pipeline.main', '--pdf', str(pdf_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=600
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


# -- API: Save (edit) a single chunk file ------------------------------------

@login_required
@require_POST
def save_chunk(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body     = json.loads(request.body)
        part     = body.get('part', '').strip()      # data sub-folder name
        filename = body.get('filename', '').strip()  # e.g. 0001_grease_gun.md
        content  = body.get('content', '')
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    if not part or not filename:
        return JsonResponse({'error': 'Missing part or filename.'}, status=400)

    # Safety: prevent path traversal
    if '..' in part or '..' in filename or '/' in filename:
        return JsonResponse({'error': 'Invalid path.'}, status=400)

    chunk_path = PROJECT_ROOT / 'vision_pipeline' / 'data' / part / 'chunks' / filename
    if not chunk_path.exists():
        return JsonResponse({'error': f'Chunk not found: {filename}'}, status=404)

    # Write the updated markdown
    chunk_path.write_text(content, encoding='utf-8')

    # Remove this chunk from Chroma so re-index picks up the new content
    try:
        import chromadb, hashlib, re as _re
        client = chromadb.PersistentClient(
            path=str(PROJECT_ROOT / 'rag_pipeline' / 'chroma_db')
        )
        col      = client.get_collection('catalog_products')
        all_data = col.get(include=['metadatas'])
        stem     = Path(filename).stem  # e.g. 0001_grease_gun
        ids_to_del = [
            doc_id for doc_id, meta in zip(all_data['ids'], all_data['metadatas'])
            if stem[:8] in str(meta.get('chunk_path', ''))
            or stem[:8] in doc_id
        ]
        if ids_to_del:
            col.delete(ids=ids_to_del)
    except Exception:
        pass  # Chroma cleanup is best-effort; re-index will handle it

    return JsonResponse({'message': f'{filename} saved. Re-index to apply changes.'})


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

    # 1. Delete the PDF file
    if pdf_path.exists():
        pdf_path.unlink()

    # 2. Delete splits folder
    splits_dir = PROJECT_ROOT / 'input' / 'splits' / stem
    if splits_dir.exists():
        shutil.rmtree(splits_dir)

    # 3. Delete ALL data folders: exact match + split parts (stem_custom_p*, stem_p*, etc.)
    data_root = PROJECT_ROOT / 'vision_pipeline' / 'data'
    if data_root.exists():
        for d in data_root.iterdir():
            if d.is_dir() and (d.name == stem or d.name.startswith(stem + '_') or d.name.startswith(stem + 'p')):
                shutil.rmtree(d)

    # 4. Remove from Chroma — source_pdf is stored as path variants, match all
    try:
        import chromadb
        client  = chromadb.PersistentClient(path=str(PROJECT_ROOT / 'rag_pipeline' / 'chroma_db'))
        col     = client.get_collection('catalog_products')
        all_data = col.get(include=['metadatas'])
        ids_to_del = [
            doc_id for doc_id, meta in zip(all_data['ids'], all_data['metadatas'])
            if stem in str(meta.get('source_pdf', ''))
        ]
        if ids_to_del:
            col.delete(ids=ids_to_del)
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

    data_dir = PROJECT_ROOT / 'vision_pipeline' / 'data'
    chunks   = []

    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if not d.is_dir():
                continue
            # Match exact folder OR split parts that start with this stem
            if d.name != pdf_name and not d.name.startswith(pdf_name):
                continue
            chunks_dir = d / 'chunks'
            if not chunks_dir.exists():
                continue
            for f in sorted(chunks_dir.glob('*.md')):
                chunks.append({
                    'filename': f.name,
                    'part':     d.name,
                    'content':  f.read_text(encoding='utf-8'),
                })

    return JsonResponse({'chunks': chunks})


# ── API: Get / Save API Keys ──────────────────────────────────────────────────

KEY_NAMES = ['GROQ_API_KEY', 'GEMINI_API_KEY_1', 'GEMINI_API_KEY_2', 'GEMINI_API_KEY_3']


def _env_path():
    return PROJECT_ROOT / '.env'


def _read_env_key(name):
    """Read a single key value from .env file."""
    env = _env_path()
    if not env.exists():
        return ''
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith(f'{name}='):
            return line[len(name) + 1:].strip()
    return ''


def _write_env_key(name, value):
    """Update or insert a key in the .env file."""
    env = _env_path()
    text = env.read_text() if env.exists() else ''
    lines = text.splitlines(keepends=True)
    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f'{name}='):
            new_lines.append(f'{name}={value}\n')
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f'{name}={value}\n')
    env.write_text(''.join(new_lines))


@login_required
def get_api_keys(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    from .models import ApiKey
    keys = {}
    for name in KEY_NAMES:
        try:
            obj = ApiKey.objects.get(name=name)
            val = obj.value
        except ApiKey.DoesNotExist:
            val = _read_env_key(name)
        # Mask all but last 4 chars for display
        keys[name] = ('•' * (len(val) - 4) + val[-4:]) if len(val) > 4 else ('•' * len(val))
    return JsonResponse({'keys': keys})


@login_required
@require_POST
def save_api_keys(request):
    if not request.user.is_staff:
        return JsonResponse({'error': 'Permission denied.'}, status=403)
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid request body.'}, status=400)

    from .models import ApiKey
    saved = []
    for name in KEY_NAMES:
        raw = body.get(name, '').strip()
        if not raw or set(raw) == {'•'}:
            continue  # skip unchanged masked values
        ApiKey.objects.update_or_create(name=name, defaults={'value': raw})
        _write_env_key(name, raw)
        # Also set in current process env so running pipeline picks it up
        os.environ[name] = raw
        saved.append(name)

    return JsonResponse({'message': f'Saved: {", ".join(saved) if saved else "nothing changed"}', 'saved': saved})
