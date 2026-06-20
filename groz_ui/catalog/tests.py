import json
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .model_config import SECRET_GEMINI, SECRET_OPENAI, get_secret
from .models import ApiKey, ModelConfiguration


@override_settings(MODEL_CONFIG_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
class ModelConfigurationApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="model-admin",
            password="test-password",
            is_staff=True,
        )
        self.user = get_user_model().objects.create_user(
            username="ordinary-user",
            password="test-password",
        )

    def test_staff_can_save_encrypted_keys_and_models(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "openai_api_key": "sk-openai-secret-1234",
                "gemini_api_key": "gemini-secret-5678",
                "vision_model": "gemini-2.5-pro",
                "chat_provider": "openai",
                "chat_model": "gpt-5.4-mini",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        openai_stored = ApiKey.objects.get(name=SECRET_OPENAI).value
        gemini_stored = ApiKey.objects.get(name=SECRET_GEMINI).value
        self.assertTrue(openai_stored.startswith("enc:v1:"))
        self.assertTrue(gemini_stored.startswith("enc:v1:"))
        self.assertNotIn("sk-openai-secret", openai_stored)
        self.assertEqual(get_secret(SECRET_OPENAI), "sk-openai-secret-1234")
        self.assertEqual(get_secret(SECRET_GEMINI), "gemini-secret-5678")

        config = ModelConfiguration.objects.get(singleton_id=1)
        self.assertEqual(config.embedding_model, "text-embedding-3-small")
        self.assertEqual(config.vision_model, "gemini-2.5-pro")
        self.assertEqual(config.chat_provider, "openai")
        self.assertEqual(config.chat_model, "gpt-5.4-mini")

        body = response.json()
        serialized = json.dumps(body)
        self.assertNotIn("sk-openai-secret-1234", serialized)
        self.assertNotIn("gemini-secret-5678", serialized)
        self.assertTrue(body["keys"]["openai"]["configured"])

    def test_rejects_model_from_wrong_provider(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "vision_model": "gemini-2.5-flash",
                "chat_provider": "gemini",
                "chat_model": "gpt-5.4-mini",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_non_staff_cannot_read_configuration(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin-panel/api/model-config/")
        self.assertEqual(response.status_code, 403)

    def test_get_never_returns_raw_key(self):
        self.client.force_login(self.staff)
        self.client.post(
            "/admin-panel/api/model-config/save/",
            data=json.dumps({
                "openai_api_key": "sk-never-return-this-9999",
                "vision_model": "gemini-2.5-flash",
                "chat_provider": "gemini",
                "chat_model": "gemini-2.5-flash",
            }),
            content_type="application/json",
        )
        response = self.client.get("/admin-panel/api/model-config/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("sk-never-return-this-9999", response.content.decode())
        self.assertIn("9999", response.json()["keys"]["openai"]["masked"])

    def test_clear_key_suppresses_environment_fallback(self):
        self.client.force_login(self.staff)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env-fallback"}):
            response = self.client.post(
                "/admin-panel/api/model-config/save/",
                data=json.dumps({
                    "clear_openai_key": True,
                    "vision_model": "gemini-2.5-flash",
                    "chat_provider": "gemini",
                    "chat_model": "gemini-2.5-flash",
                }),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(get_secret(SECRET_OPENAI), "")
            self.assertFalse(response.json()["keys"]["openai"]["configured"])
