from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.home, name="home"),
    path("projects/", views.project_list, name="projects"),
    path("projects/<uuid:project_id>/reports/", views.report_list, name="report-list"),
    path("projects/<uuid:project_id>/reports/create/", views.report_create, name="report-create"),
    path("reports/<uuid:report_id>/", views.report_detail, name="report-detail"),
    path("reports/<uuid:report_id>/versions/create/", views.version_create, name="version-create"),
    path("versions/<uuid:version_id>/", views.version_detail, name="version-detail"),
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
