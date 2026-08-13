from django.urls import path

from . import views

app_name = "topvisor"
urlpatterns = [
    path("projects/<uuid:project_id>/", views.connection, name="connection"),
    path("projects/<uuid:project_id>/sync/", views.sync, name="sync"),
    path(
        "projects/<uuid:project_id>/runs/<int:run_id>/delete/", views.delete_run, name="delete-run"
    ),
    path(
        "projects/<uuid:project_id>/runs/delete-failed/",
        views.delete_failed_runs,
        name="delete-failed-runs",
    ),
    path("projects/<uuid:project_id>/delete/", views.delete_connection, name="delete-connection"),
]
