from django.contrib import admin

from .models import KeywordPosition, RankingSnapshot


@admin.register(RankingSnapshot)
class RankingSnapshotAdmin(admin.ModelAdmin):
    list_display = [
        "project",
        "snapshot_date",
        "search_engine",
        "region",
        "tracked_keyword_count",
    ]
    list_filter = ["search_engine"]
    search_fields = ["project__name", "region"]
    readonly_fields = [
        "project",
        "import_batch",
        "snapshot_date",
        "search_engine",
        "region",
        "tracked_keyword_count",
        "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KeywordPosition)
class KeywordPositionAdmin(admin.ModelAdmin):
    list_display = [
        "query",
        "ranking_snapshot",
        "position_raw",
        "position_status",
        "frequency",
        "group_name",
    ]
    list_filter = ["position_status"]
    search_fields = ["query", "normalized_query", "group_name", "target_url"]
    readonly_fields = [
        "ranking_snapshot",
        "query",
        "normalized_query",
        "frequency",
        "position_raw",
        "position_value",
        "position_status",
        "group_name",
        "target_url",
        "normalized_target_url",
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
