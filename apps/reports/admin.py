from django.contrib import admin
from django.utils import timezone

from .models import (
    GeneratedArtifact,
    NarrativeBlock,
    Report,
    ReportDatasetSnapshot,
    ReportVersion,
    ValidationIssue,
)


@admin.register(GeneratedArtifact)
class GeneratedArtifactAdmin(admin.ModelAdmin):
    list_display = ["filename", "artifact_type", "status", "report_version", "size", "created_at"]
    list_filter = ["artifact_type", "status", "is_draft"]
    readonly_fields = [field.name for field in GeneratedArtifact._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD", "OPTIONS")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["project", "report_month", "created_at"]
    list_filter = ["report_month"]
    search_fields = ["project__name", "project__domain"]

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.versions.exists():
            return ["project", "report_month", "created_at"]
        return ["created_at"]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReportVersion)
class ReportVersionAdmin(admin.ModelAdmin):
    list_display = ["report", "number", "created_by", "created_at"]
    readonly_fields = ["report", "number", "created_by", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReportDatasetSnapshot)
class ReportDatasetSnapshotAdmin(admin.ModelAdmin):
    list_display = ["version", "schema_version", "formula_version", "checksum", "created_at"]
    readonly_fields = [
        "version",
        "schema_version",
        "formula_version",
        "payload",
        "checksum",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD", "OPTIONS")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NarrativeBlock)
class NarrativeBlockAdmin(admin.ModelAdmin):
    list_display = ["report_version", "section_code", "kind", "status", "sort_order", "updated_at"]
    list_filter = ["kind", "status", "section_code"]
    search_fields = ["report_version__report__project__name", "generated_text", "edited_text"]
    readonly_fields = [
        "report_version",
        "section_code",
        "kind",
        "generated_text",
        "facts",
        "sort_order",
        "confirmed_by",
        "confirmed_at",
        "created_at",
        "updated_at",
    ]
    fields = [
        "report_version",
        "section_code",
        "kind",
        "generated_text",
        "edited_text",
        "facts",
        "status",
        "confirmed_by",
        "confirmed_at",
        "sort_order",
        "created_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if obj.status == NarrativeBlock.Status.CONFIRMED:
            if obj.confirmed_at is None:
                obj.confirmed_by = request.user
                obj.confirmed_at = timezone.now()
        else:
            obj.confirmed_by = None
            obj.confirmed_at = None
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = ["code", "severity", "section_code", "version", "created_at"]
    list_filter = ["severity", "code"]
    readonly_fields = [
        "version",
        "code",
        "severity",
        "section_code",
        "message",
        "details",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD", "OPTIONS")

    def has_delete_permission(self, request, obj=None):
        return False
