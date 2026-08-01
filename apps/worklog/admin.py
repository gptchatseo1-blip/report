from django.contrib import admin

from .models import WorkCategory, WorkLogItem


@admin.register(WorkCategory)
class WorkCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "project", "sort_order", "active"]
    list_filter = ["active", "project"]
    search_fields = ["name", "project__name"]


@admin.register(WorkLogItem)
class WorkLogItemAdmin(admin.ModelAdmin):
    list_display = ["work_date", "title", "project", "category", "status", "responsible"]
    list_filter = ["status", "project", "category"]
    search_fields = ["title", "comment", "responsible", "url", "result_url"]
