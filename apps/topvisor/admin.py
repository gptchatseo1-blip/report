from django.contrib import admin

from .models import TopvisorConnection, TopvisorProjectMapping


@admin.register(TopvisorConnection)
class TopvisorConnectionAdmin(admin.ModelAdmin):
    list_display = ["project", "user_id", "key_status", "last_verified_at", "updated_at"]
    search_fields = ["project__name", "user_id"]
    readonly_fields = [
        "project",
        "user_id",
        "key_status",
        "last_verified_at",
        "created_at",
        "updated_at",
    ]
    exclude = ["api_key_encrypted", "api_key_last_four"]

    @admin.display(description="API-ключ")
    def key_status(self, obj):
        return "Ключ настроен" if obj.api_key_encrypted else "Не настроен"


@admin.register(TopvisorProjectMapping)
class TopvisorProjectMappingAdmin(admin.ModelAdmin):
    list_display = ["project", "topvisor_project_name", "topvisor_project_id", "last_checked_at"]
    search_fields = ["project__name", "topvisor_project_name", "topvisor_project_id"]
    readonly_fields = ["last_checked_at", "created_at", "updated_at"]
