from django.contrib import admin

from .models import Report, ReportDatasetSnapshot, ReportVersion, ValidationIssue


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["project", "report_month", "created_at"]
    list_filter = ["report_month"]
    search_fields = ["project__name", "project__domain"]

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


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = ["code", "severity", "version", "created_at"]
    list_filter = ["severity", "code"]
    readonly_fields = ["version", "code", "severity", "message", "details", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in ("GET", "HEAD", "OPTIONS")

    def has_delete_permission(self, request, obj=None):
        return False
