import json
from django.conf import settings
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

PROJECT_ROOT = settings.PROJECT_ROOT


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
        from rag_pipeline.retriever import Retriever
        from rag_pipeline.llm import LLMAnswerer

        runtime = get_runtime_config()
        if not runtime.openai_api_key:
            return JsonResponse({'error': 'Search is not configured. Ask an admin to add the OpenAI key.'}, status=503)
        if not runtime.chat_api_key:
            return JsonResponse({'error': 'Chat model is not configured. Ask an admin to add its API key.'}, status=503)
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
