import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


PREFIX = "enc:v1:"


def _fernet():
    configured = str(settings.MODEL_CONFIG_ENCRYPTION_KEY).encode("utf-8")
    try:
        return Fernet(configured)
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(configured).digest()))


def encrypt_existing_keys(apps, schema_editor):
    ApiKey = apps.get_model("catalog", "ApiKey")
    cipher = _fernet()
    for key in ApiKey.objects.exclude(value=""):
        if not key.value.startswith(PREFIX):
            key.value = PREFIX + cipher.encrypt(key.value.encode("utf-8")).decode("ascii")
            key.save(update_fields=["value"])


def decrypt_existing_keys(apps, schema_editor):
    ApiKey = apps.get_model("catalog", "ApiKey")
    cipher = _fernet()
    for key in ApiKey.objects.filter(value__startswith=PREFIX):
        token = key.value[len(PREFIX):].encode("ascii")
        key.value = cipher.decrypt(token).decode("utf-8")
        key.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ModelConfiguration",
            fields=[
                ("singleton_id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("embedding_model", models.CharField(default="text-embedding-3-small", editable=False, max_length=64)),
                ("vision_model", models.CharField(default="gemini-2.5-flash", max_length=80)),
                ("chat_provider", models.CharField(default="gemini", max_length=16)),
                ("chat_model", models.CharField(default="gemini-2.5-flash", max_length=80)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_model_configurations", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.RunPython(encrypt_existing_keys, decrypt_existing_keys),
    ]
