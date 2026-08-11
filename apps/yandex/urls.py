from django.urls import path

from . import views

app_name = "yandex"
urlpatterns = [
    path("projects/<uuid:project_id>/", views.connection, name="connection"),
    path("projects/<uuid:project_id>/oauth/start/", views.oauth_start, name="oauth-start"),
    path("oauth/callback/", views.oauth_callback, name="oauth-callback"),
    path("projects/<uuid:project_id>/counter/", views.select_counter, name="select-counter"),
    path("projects/<uuid:project_id>/goals/", views.select_goals, name="select-goals"),
    path("projects/<uuid:project_id>/host/", views.select_host, name="select-host"),
    path("projects/<uuid:project_id>/sync/", views.sync, name="sync"),
    path(
        "projects/<uuid:project_id>/webmaster/sync/",
        views.sync_webmaster_view,
        name="sync-webmaster",
    ),
    path("connections/<uuid:connection_id>/disconnect/", views.disconnect, name="disconnect"),
]
