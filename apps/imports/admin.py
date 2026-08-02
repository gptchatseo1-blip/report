from django.contrib import admin

from .models import ImportBatch, ImportRowError


class ImportRowErrorInline(admin.TabularInline):
    model = ImportRowError
    extra = 0
    can_delete = False
    readonly_fields = ["row_number", "code", "message", "raw_values"]


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = [
        "original_filename",
        "project",
        "snapshot_date",
        "search_engine",
        "region",
        "ranking_depth",
        "status",
        "valid_rows",
        "error_rows",
    ]
    list_filter = ["status", "search_engine"]
    search_fields = ["original_filename", "project__name", "region", "file_checksum"]
    readonly_fields = [
        "project",
        "kind",
        "original_filename",
        "source_file",
        "file_checksum",
        "status",
        "snapshot_date",
        "search_engine",
        "region",
        "ranking_depth",
        "total_rows",
        "valid_rows",
        "error_rows",
        "preview_payload",
        "error_summary",
        "uploaded_by",
        "created_at",
        "updated_at",
        "confirmed_at",
    ]
    inlines = [ImportRowErrorInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
