import json
import logging
import uuid
from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

PROJECT_ROOT = settings.PROJECT_ROOT
logger = logging.getLogger(__name__)


def home(request):
    return render(request, 'chatbot_app/home.html')


def chat_home(request):
    return render(request, 'chatbot_app/chat.html')


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

    try:
        from catalog.model_config import get_runtime_config
        from catalog.services.query_engine import CatalogQueryEngine

        runtime = get_runtime_config()
        # if not runtime.openai_api_key:
        #     return JsonResponse({'error': 'Search is not configured. Ask an admin to add the OpenAI key.'}, status=503)
        if not runtime.groq_api_key:
            return JsonResponse({'error': 'Search is not configured. Ask an admin to add the Groq key.'}, status=503)
        if not runtime.chat_api_key:
            return JsonResponse({'error': 'Chat model is not configured. Ask an admin to add its API key.'}, status=503)

        catalog_ids = body.get('catalog_ids', [])
        document_ids = body.get('document_ids', [])
        if not isinstance(catalog_ids, list) or not isinstance(document_ids, list):
            return JsonResponse({'error': 'catalog_ids and document_ids must be arrays.'}, status=400)
        try:
            page = max(1, int(body.get('page', 1)))
            page_size = min(100, max(1, int(body.get('page_size', 50))))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'Invalid pagination values.'}, status=400)
        execution = CatalogQueryEngine(runtime).execute(
            query,
            catalog_ids=catalog_ids,
            document_ids=document_ids,
            page=page,
            page_size=page_size,
        )
        payload = execution.as_dict()
        payload['query_path'] = 'v2'
        logger.info(
            'Chat response generated | provider=%s model=%s query=%r results=%d',
            runtime.chat_provider,
            runtime.chat_model,
            query,
            len(payload.get('results', [])),
        )
        return JsonResponse(payload)
    except Exception:
        correlation_id = str(uuid.uuid4())
        logger.exception('Catalog query failed; correlation_id=%s', correlation_id)
        return JsonResponse({
            'error': 'Catalog query failed.',
            'correlation_id': correlation_id,
        }, status=500)
