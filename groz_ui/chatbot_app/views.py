import json
import logging
import uuid
from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from catalog.services.chat_memory import (
    PUBLIC_CHAT_MEMORY_KEY,
    build_memory_update,
    clear_turn,
    load_turn,
    save_turn,
)
from rag_pipeline.ai_router import AIQueryRouterError

PROJECT_ROOT = settings.PROJECT_ROOT
logger = logging.getLogger(__name__)


@ensure_csrf_cookie
def home(request):
    if request.user.is_authenticated:
        logout(request)
    return render(request, 'chatbot_app/home.html')


def chat_home(request):
    from django.db.models import Count, Q
    from catalog.models import ProductFamily, DocumentChunk
    
    # Icon mapping
    category_icons = {
        'lubrication': 'fa-oil-can',
        'grease': 'fa-pump-soap',
        'hammer': 'fa-hammer',
        'measuring': 'fa-ruler',
        'measurement': 'fa-ruler',
        'led': 'fa-lightbulb',
        'lighting': 'fa-lightbulb',
        'light': 'fa-lightbulb',
        'fluid': 'fa-tint',
        'precision': 'fa-drafting-compass',
        'tool': 'fa-tools',
        'wrench': 'fa-wrench',
        'screw': 'fa-screwdriver',
        'drill': 'fa-drill',
        'valve': 'fa-cog',
        'pump': 'fa-pump-soap',
        'bearing': 'fa-cog',
        'gun': 'fa-tools',
        'kit': 'fa-toolbox',
        'equipment': 'fa-cogs',
        'accessory': 'fa-puzzle-piece',
        'additions': 'fa-plus-circle',
        'packer': 'fa-box',
        'lever': 'fa-hand-pointer',
        'pistol': 'fa-hand-rock',
        'swivel': 'fa-sync',
    }
    
    # Get ALL product families that have indexed chunks
    product_families = (
        ProductFamily.objects
        .filter(
            document__is_active=True,
            chunks__index_status='indexed'
        )
        .distinct()
        .values('id', 'product_name', 'raw_category')
        .order_by('product_name')
    )
    
    category_list = []
    seen_names = set()
    
    for family in product_families:
        prod_name = family['product_name'].strip()
        if not prod_name:
            continue
        # Normalize: lowercase, strip punctuation/spaces for dedup
        import re
        norm = re.sub(r'[^a-z0-9]', '', prod_name.lower())
        if norm in seen_names:
            continue
        seen_names.add(norm)
        
        icon = 'fa-tools'
        search_text = (prod_name + ' ' + (family['raw_category'] or '')).lower()
        for keyword, icon_class in category_icons.items():
            if keyword in search_text:
                icon = icon_class
                break
        
        category_list.append({
            'name': prod_name,
            'icon': icon,
            'count': 1
        })
    
    return render(request, 'chatbot_app/chat.html', {'categories': category_list})


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

        memory_turn = load_turn(request.session, PUBLIC_CHAT_MEMORY_KEY)
        runtime = get_runtime_config()
        if not runtime.embedding_api_key:
            provider_label = 'Gemini' if runtime.embedding_provider == 'gemini' else 'OpenAI'
            return JsonResponse({'error': f'Search is not configured. Ask an admin to add the {provider_label} key.'}, status=503)
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
                clear_turn(request.session, PUBLIC_CHAT_MEMORY_KEY)
            else:
                save_turn(request.session, PUBLIC_CHAT_MEMORY_KEY, memory_update)
        except Exception:
            logger.exception('Failed to persist public chat memory.')
        logger.info(
            'Chat response generated | provider=%s model=%s query=%r results=%d',
            runtime.chat_provider,
            runtime.chat_model,
            query,
            len(payload.get('sources', [])),
        )
        return JsonResponse(payload)
    except AIQueryRouterError:
        correlation_id = str(uuid.uuid4())
        logger.exception('AI query router failed; correlation_id=%s', correlation_id)
        return JsonResponse({
            'error': 'AI query router failed.',
            'correlation_id': correlation_id,
        }, status=502)
    except Exception:
        correlation_id = str(uuid.uuid4())
        logger.exception('Catalog query failed; correlation_id=%s', correlation_id)
        return JsonResponse({
            'error': 'Catalog query failed.',
            'correlation_id': correlation_id,
        }, status=500)
