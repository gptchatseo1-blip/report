from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import path


def health(request):
    connection.ensure_connection()
    return JsonResponse({"status": "ok"})


urlpatterns = [path("admin/", admin.site.urls), path("health/", health, name="health")]
