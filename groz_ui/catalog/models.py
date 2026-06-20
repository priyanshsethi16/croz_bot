from django.db import models
from django.conf import settings


class ApiKey(models.Model):
    name       = models.CharField(max_length=64, unique=True)
    value      = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class ModelConfiguration(models.Model):
    """Singleton configuration for catalog AI providers and model routing."""

    singleton_id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    embedding_model = models.CharField(max_length=64, default='text-embedding-3-small', editable=False)
    vision_model = models.CharField(max_length=80, default='gemini-2.5-flash')
    chat_provider = models.CharField(max_length=16, default='gemini')
    chat_model = models.CharField(max_length=80, default='gemini-2.5-flash')
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_model_configurations',
    )

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return 'Catalog model configuration'
