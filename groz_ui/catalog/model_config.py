"""Encrypted model/key configuration shared by Django and pipeline subprocesses."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


EMBEDDING_MODEL = "text-embedding-3-small"

VISION_MODELS = (
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("gemini-2.0-flash", "Gemini 2.0 Flash"),
)

CHAT_MODELS = {
    "gemini": (
        ("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
        ("gemini-2.0-flash", "Gemini 2.0 Flash"),
    ),
    "openai": (
        ("gpt-4o", "GPT-4o"),
        ("gpt-4o-mini", "GPT-4o mini"),
        ("gpt-4.1", "GPT-4.1"),
        ("gpt-4.1-mini", "GPT-4.1 mini"),
    ),
}

DEFAULT_CHAT_MODEL = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}

SECRET_OPENAI = "OPENAI_API_KEY"
SECRET_GROQ = "GROQ_API_KEY"
SECRET_GEMINI = "GEMINI_API_KEY"
_ENCRYPTED_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    configured = str(settings.MODEL_CONFIG_ENCRYPTION_KEY).encode("utf-8")
    try:
        return Fernet(configured)
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(configured).digest())
        return Fernet(derived)


def encrypt_secret(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _ENCRYPTED_PREFIX + token


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_ENCRYPTED_PREFIX):
        return value  # legacy plaintext; replaced with ciphertext on the next save
    try:
        token = value[len(_ENCRYPTED_PREFIX):].encode("ascii")
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        raise ValueError("Stored API key cannot be decrypted. Check MODEL_CONFIG_ENCRYPTION_KEY.")


def _secret_from_db(name: str) -> tuple[bool, str]:
    from .models import ApiKey

    try:
        return True, decrypt_secret(ApiKey.objects.get(name=name).value)
    except ApiKey.DoesNotExist:
        return False, ""
    except ValueError:
        # Decryption failed (key rotated/changed) — clear the corrupted entry
        ApiKey.objects.filter(name=name).update(value="")
        return False, ""


def get_secret(name: str) -> str:
    found, value = _secret_from_db(name)
    if found:
        return value
    if name == SECRET_GEMINI:
        return (
            os.getenv(SECRET_GEMINI, "").strip()
            or os.getenv("GEMINI_API_KEY_1", "").strip()
        )
    return os.getenv(name, "").strip()


def save_secret(name: str, value: str) -> None:
    from .models import ApiKey

    ApiKey.objects.update_or_create(name=name, defaults={"value": encrypt_secret(value)})


def clear_secret(name: str) -> None:
    from .models import ApiKey

    ApiKey.objects.update_or_create(name=name, defaults={"value": ""})


def mask_secret(value: str) -> str:
    if not value:
        return ""
    tail = value[-4:] if len(value) >= 4 else ""
    return f"••••••••{tail}"


def get_model_configuration():
    from .models import ModelConfiguration

    config, _ = ModelConfiguration.objects.get_or_create(singleton_id=1)
    return config


@dataclass(frozen=True)
class RuntimeModelConfig:
    openai_api_key: str
    groq_api_key: str
    gemini_api_key: str
    embedding_model: str
    vision_model: str
    chat_provider: str
    chat_model: str

    @property
    def chat_api_key(self) -> str:
        if self.chat_provider == "openai":
            return self.openai_api_key
        return self.gemini_api_key


def get_runtime_config() -> RuntimeModelConfig:
    config = get_model_configuration()
    return RuntimeModelConfig(
        openai_api_key=get_secret(SECRET_OPENAI),
        groq_api_key=get_secret(SECRET_GROQ),
        gemini_api_key=get_secret(SECRET_GEMINI),
        embedding_model=EMBEDDING_MODEL,
        vision_model=config.vision_model,
        chat_provider=config.chat_provider,
        chat_model=config.chat_model,
    )


def configuration_payload() -> dict:
    config = get_model_configuration()
    openai_key = get_secret(SECRET_OPENAI)
    groq_key = get_secret(SECRET_GROQ)
    gemini_key = get_secret(SECRET_GEMINI)
    return {
        "configuration": {
            "embedding_model": EMBEDDING_MODEL,
            "vision_model": config.vision_model,
            "chat_provider": config.chat_provider,
            "chat_model": config.chat_model,
        },
        "keys": {
            "openai": {"configured": bool(openai_key), "masked": mask_secret(openai_key)},
            "groq": {"configured": bool(groq_key), "masked": mask_secret(groq_key)},
            "gemini": {"configured": bool(gemini_key), "masked": mask_secret(gemini_key)},
        },
        "options": {
            "vision_models": [{"value": value, "label": label} for value, label in VISION_MODELS],
            "chat_models": {
                provider: [{"value": value, "label": label} for value, label in models]
                for provider, models in CHAT_MODELS.items()
            },
        },
    }


def update_configuration(payload: dict, user=None) -> dict:
    config = get_model_configuration()

    vision_model = str(payload.get("vision_model", config.vision_model)).strip()
    allowed_vision = {value for value, _ in VISION_MODELS}
    if vision_model not in allowed_vision:
        vision_model = "gemini-2.5-flash"  # safe default

    chat_provider = str(payload.get("chat_provider", config.chat_provider)).strip().lower()
    if chat_provider not in CHAT_MODELS:
        raise ValueError("Chat provider must be gemini or openai.")

    chat_model = str(payload.get("chat_model", "")).strip() or DEFAULT_CHAT_MODEL[chat_provider]
    allowed_chat = {value for value, _ in CHAT_MODELS[chat_provider]}
    if chat_model not in allowed_chat:
        raise ValueError(f"Unsupported {chat_provider} chat model.")

    if payload.get("clear_openai_key"):
        clear_secret(SECRET_OPENAI)
    elif str(payload.get("openai_api_key", "")).strip():
        save_secret(SECRET_OPENAI, str(payload["openai_api_key"]))
    if payload.get("clear_groq_key"):
        clear_secret(SECRET_GROQ)
    elif str(payload.get("groq_api_key", "")).strip():
        save_secret(SECRET_GROQ, str(payload["groq_api_key"]))

    if payload.get("clear_gemini_key"):
        clear_secret(SECRET_GEMINI)
    elif str(payload.get("gemini_api_key", "")).strip():
        save_secret(SECRET_GEMINI, str(payload["gemini_api_key"]))

    config.embedding_model = EMBEDDING_MODEL
    config.vision_model = vision_model
    config.chat_provider = chat_provider
    config.chat_model = chat_model
    config.updated_by = user if getattr(user, "is_authenticated", False) else None
    config.save()
    return configuration_payload()


def subprocess_environment() -> dict[str, str]:
    runtime = get_runtime_config()
    env = os.environ.copy()
    if runtime.openai_api_key:
        env[SECRET_OPENAI] = runtime.openai_api_key
    if runtime.groq_api_key:
        env[SECRET_GROQ] = runtime.groq_api_key
    if runtime.gemini_api_key:
        env[SECRET_GEMINI] = runtime.gemini_api_key
    env["OPENAI_EMBEDDING_MODEL"] = runtime.embedding_model
    env["GEMINI_VISION_MODEL"] = runtime.vision_model
    env["CHAT_PROVIDER"] = runtime.chat_provider
    env["CHAT_MODEL"] = runtime.chat_model
    return env
