from django.urls import path

from . import views

app_name = "metrics"

urlpatterns = [path("synthetic/", views.synthetic_sync, name="synthetic_sync")]
