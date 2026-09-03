from django.urls import path

from . import views

app_name = "serphunt"
urlpatterns = [
    path("settings/", views.credentials, name="credentials"),
    path("projects/<uuid:project_id>/", views.connection, name="connection"),
    path("projects/<uuid:project_id>/sync/", views.sync, name="sync"),
]
