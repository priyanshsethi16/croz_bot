from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage


def test_openai_embedding_factory_uses_required_model():
    with patch("rag_pipeline.providers.OpenAIEmbeddings") as embeddings_cls:
        from rag_pipeline.providers import build_embeddings

        build_embeddings("sk-test")
        embeddings_cls.assert_called_once_with(
            model="text-embedding-3-small",
            api_key="sk-test",
            chunk_size=100,
            max_retries=3,
        )


def test_openai_chat_answer_uses_langchain():
    with patch("rag_pipeline.llm.ChatOpenAI") as chat_cls:
        chat_cls.return_value.invoke.return_value = AIMessage(content="Grounded answer")
        from rag_pipeline.llm import LLMAnswerer

        answerer = LLMAnswerer("openai", "sk-test", "gpt-5.4-mini")
        result = answerer.answer("question", [{
            "text": "# Product",
            "metadata": {"product_name": "Product", "product_code": "P1"},
            "score": 0.9,
        }])

        assert result == "Grounded answer"
        chat_cls.assert_called_once_with(model="gpt-5.4-mini", api_key="sk-test", max_retries=3)
        chat_cls.return_value.invoke.assert_called_once()


def test_gemini_vision_uses_langchain_multimodal_message(tmp_path):
    png = tmp_path / "page.png"
    png.write_bytes(b"fake-png")
    model = Mock()
    model.invoke.return_value = AIMessage(content="[]")

    with patch("langchain_google_genai.ChatGoogleGenerativeAI", return_value=model):
        from vision_pipeline.gemini_extractor import GeminiExtractor

        extractor = GeminiExtractor("gemini-test", {
            "gemini_model": "gemini-2.5-flash",
            "request_delay": 0,
        })
        assert extractor.extract_page(png, 1) == []

    message = model.invoke.call_args.args[0][0]
    assert message.content[0]["type"] == "text"
    assert message.content[1]["type"] == "image_url"
    assert message.content[1]["image_url"].startswith("data:image/png;base64,")
