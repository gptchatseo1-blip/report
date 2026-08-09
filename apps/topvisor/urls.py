from django.urls import path

from . import views

app_name = "topvisor"
urlpatterns = [
    path("projects/<uuid:project_id>/", views.connection, name="connection"),
    path("projects/<uuid:project_id>/sync/", views.sync, name="sync"),
]
