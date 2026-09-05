from django.urls import path

from . import views
from .topvisor_editor_maintenance import topvisor_editor_clear, topvisor_editor_refresh

app_name = "reports"

urlpatterns = [
    path("", views.home, name="home"),
    path("projects/", views.project_list, name="projects"),
    path("projects/faq/", views.project_faq, name="project-faq"),
    path("projects/create/", views.project_create, name="project-create"),
    path("projects/<uuid:project_id>/delete/", views.project_delete, name="project-delete"),
    path("projects/<uuid:project_id>/reports/", views.report_list, name="report-list"),
    path("projects/<uuid:project_id>/reports/create/", views.report_create, name="report-create"),
    path(
        "projects/<uuid:project_id>/reports/settings/",
        views.report_settings_save,
        name="report-settings-save",
    ),
    path(
        "projects/<uuid:project_id>/reports/topvisor-editor/refresh/",
        topvisor_editor_refresh,
        name="topvisor-editor-refresh",
    ),
    path(
        "projects/<uuid:project_id>/reports/topvisor-editor/clear/",
        topvisor_editor_clear,
        name="topvisor-editor-clear",
    ),
    path(
        "projects/<uuid:project_id>/reports/source-history/clear/",
        views.source_history_clear,
        name="source-history-clear",
    ),
    path("reports/<uuid:report_id>/", views.report_detail, name="report-detail"),
    path("reports/<uuid:report_id>/versions/create/", views.version_create, name="version-create"),
    path("versions/<uuid:version_id>/", views.version_detail, name="version-detail"),
    path("versions/<uuid:version_id>/delete/", views.version_delete, name="version-delete"),
    path("versions/<uuid:version_id>/validate/", views.version_validate, name="validate"),
    path(
        "versions/<uuid:version_id>/export/<str:artifact_type>/",
        views.artifact_generate,
        name="artifact-generate",
    ),
    path(
        "artifacts/<uuid:artifact_id>/download/", views.artifact_download, name="artifact-download"
    ),
    path("artifacts/<uuid:artifact_id>/delete/", views.artifact_delete, name="artifact-delete"),
    path("narratives/<uuid:block_id>/edit/", views.narrative_edit, name="narrative-edit"),
    path("narratives/<uuid:block_id>/reset/", views.narrative_reset, name="narrative-reset"),
    path("narratives/<uuid:block_id>/confirm/", views.narrative_confirm, name="narrative-confirm"),
]
