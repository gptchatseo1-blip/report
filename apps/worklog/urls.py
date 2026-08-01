from django.urls import path

from . import views

app_name = "worklog"

urlpatterns = [
    path("", views.worklog_list, name="list"),
    path("new/", views.worklog_create, name="create"),
    path("categories/new/", views.category_create, name="category_create"),
]
