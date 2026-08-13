from django.contrib import admin

from .models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexMetrikaSyncRun,
    YandexOAuthCredential,
    YandexWebmasterProjectMapping,
    YandexWebmasterSyncRun,
)


@admin.register(YandexOAuthCredential)
class YandexOAuthCredentialAdmin(admin.ModelAdmin):
    list_display = ("client_id", "secret_status", "redirect_uri", "updated_at")
    readonly_fields = ("client_id", "secret_status", "redirect_uri", "created_at", "updated_at")
    exclude = ("client_secret_encrypted", "client_secret_last_four")

    @admin.display(description="Client secret")
    def secret_status(self, obj):
        return "Настроен" if obj.client_secret_encrypted else "Не настроен"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {"GET", "HEAD", "OPTIONS"} and super().has_change_permission(
            request, obj
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "show_save": False,
            "show_save_and_add_another": False,
            "show_save_and_continue": False,
        }
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(YandexConnection)
class YandexConnectionAdmin(admin.ModelAdmin):
    list_display = ("account_login", "user", "active", "updated_at")
    exclude = ("access_token_encrypted", "refresh_token_encrypted")
    readonly_fields = (
        "user",
        "account_id",
        "account_login",
        "expires_at",
        "scopes",
        "active",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


admin.site.register(YandexMetrikaProjectMapping)
admin.site.register(YandexMetrikaSyncRun)
admin.site.register(YandexWebmasterProjectMapping)
admin.site.register(YandexWebmasterSyncRun)
