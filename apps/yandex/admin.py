from django.contrib import admin

from .models import YandexConnection, YandexMetrikaProjectMapping, YandexMetrikaSyncRun


@admin.register(YandexConnection)
class YandexConnectionAdmin(admin.ModelAdmin):
    list_display = ("account_login", "user", "active", "updated_at")
    exclude = ("access_token_encrypted", "refresh_token_encrypted")
    readonly_fields = (
        "user",
        "account_id",
        "account_login",
        "expires_at",
        "active",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


admin.site.register(YandexMetrikaProjectMapping)
admin.site.register(YandexMetrikaSyncRun)
