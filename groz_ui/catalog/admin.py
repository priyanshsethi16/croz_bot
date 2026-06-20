from django.contrib import admin

from .models import ApiKey, ModelConfiguration


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ('name', 'updated_at')
    readonly_fields = ('value', 'updated_at')


@admin.register(ModelConfiguration)
class ModelConfigurationAdmin(admin.ModelAdmin):
    list_display = ('embedding_model', 'vision_model', 'chat_provider', 'chat_model', 'updated_at')
    readonly_fields = ('embedding_model', 'updated_at', 'updated_by')
