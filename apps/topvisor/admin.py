from django.contrib import admin

from .models import TopvisorProjectMapping


@admin.register(TopvisorProjectMapping)
class TopvisorProjectMappingAdmin(admin.ModelAdmin):
    list_display = ["project", "topvisor_project_name", "topvisor_project_id", "last_checked_at"]
    search_fields = ["project__name", "topvisor_project_name", "topvisor_project_id"]
    readonly_fields = ["last_checked_at", "created_at", "updated_at"]
