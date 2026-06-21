import json
import os
from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST
from dotenv import load_dotenv

PROJECT_ROOT = settings.PROJECT_ROOT
load_dotenv(PROJECT_ROOT / '.env')

# ── Module-level shared retriever (loaded once) ───────────────────────────────
_retriever = None

def _get_retriever():
    global _retriever
    if _retriever is None:
        from rag_pipeline.retriever import HybridRetriever
        _retriever = HybridRetriever(top_k=5)
    return _retriever


# ── Per-session LLMAnswerer (holds ConversationBufferWindowMemory k=1) ─────────
_SESSION_LLM_KEY = 'rag_llm_session'

def _get_session_llm(request):
    """Return an LLMAnswerer bound to this HTTP session."""
    from rag_pipeline.llm import LLMAnswerer
    if not hasattr(request, '_llm_instance'):
        # Store in process memory keyed by session_key (simple per-worker approach)
        session_key = request.session.session_key or 'anon'
        if not hasattr(_get_session_llm, '_cache'):
            _get_session_llm._cache = {}
        if session_key not in _get_session_llm._cache:
            _get_session_llm._cache[session_key] = LLMAnswerer(os.getenv('GROQ_API_KEY', ''))
        request._llm_instance = _get_session_llm._cache[session_key]
    return request._llm_instance


def _get_catalog_meta():
    """Return unique product names and categories from the chroma vector store."""
    try:
        from rag_pipeline.ingest import CHROMA_DIR, COLLECTION, _ONNXEmbeddings
        from langchain_chroma import Chroma
        vs = Chroma(collection_name=COLLECTION,
                    embedding_function=_ONNXEmbeddings(),
                    persist_directory=CHROMA_DIR)
        data = vs.get(include=['metadatas'])
        names = sorted(set(
            m.get('product_name', '') for m in data['metadatas']
            if m.get('product_name')
        ))
        categories = sorted(set(
            m.get('category', '') for m in data['metadatas']
            if m.get('category')
        ))
        return names, categories
    except Exception:
        return [], []


def home(request):
    return render(request, 'chatbot_app/home.html')


@ensure_csrf_cookie
def chat_home(request):
    if not request.session.session_key:
        request.session.create()
    product_names, categories = _get_catalog_meta()
    return render(request, 'chatbot_app/chat.html', {
        'product_names': product_names,
        'categories': categories,
    })


def user_logout(request):
    logout(request)
    return redirect('/admin-panel/login/')


@require_POST
def chat_query(request):
    try:
        body  = json.loads(request.body)
        query = body.get('query', '').strip()
    except Exception:
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    if not query:
        return JsonResponse({'error': 'Empty query.'}, status=400)

    # Handle memory clear command
    if query.lower() == '/clear':
        llm = _get_session_llm(request)
        llm.clear_memory()
        return JsonResponse({'answer': 'Conversation memory cleared.', 'sources': []})

    try:
        retriever = _get_retriever()
        llm       = _get_session_llm(request)
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
