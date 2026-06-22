import os
from django.apps import AppConfig


class ChatbotAppConfig(AppConfig):
    name = 'chatbot_app'

    def ready(self):
        # Only run in the main process (not the reloader watcher process)
        if os.environ.get('RUN_MAIN') != 'true':
            return
        try:
            from rag_pipeline.retriever import HybridRetriever
            import chatbot_app.views as v
            if v._retriever is None:
                v._retriever = HybridRetriever(top_k=5)
        except Exception:
            pass
