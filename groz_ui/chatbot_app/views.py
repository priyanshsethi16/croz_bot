import json
import os
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
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / '.env')
        from rag_pipeline.retriever import Retriever
        from rag_pipeline.llm import LLMAnswerer

        retriever = Retriever(top_k=5)
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
