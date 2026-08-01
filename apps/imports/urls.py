from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.import_list, name="list"),
    path("template.csv", views.import_template, name="template"),
    path("upload/", views.import_upload, name="upload"),
    path("<uuid:batch_id>/", views.import_detail, name="detail"),
    path("<uuid:batch_id>/confirm/", views.import_confirm, name="confirm"),
]
