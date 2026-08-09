from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    connection.ensure_connection()
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", include("apps.reports.urls")),
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("imports/", include("apps.imports.urls")),
    path("metrics/", include("apps.metrics.urls")),
    path("topvisor/", include("apps.topvisor.urls")),
    path("worklog/", include("apps.worklog.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
