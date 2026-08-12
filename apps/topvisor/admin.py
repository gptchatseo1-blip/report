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


@admin.register(TopvisorProjectMapping)
class TopvisorProjectMappingAdmin(admin.ModelAdmin):
    list_display = ["project", "topvisor_project_name", "topvisor_project_id", "last_checked_at"]
    search_fields = ["project__name", "topvisor_project_name", "topvisor_project_id"]
    readonly_fields = ["last_checked_at", "created_at", "updated_at"]
