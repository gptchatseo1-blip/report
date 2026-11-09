import io
import logging

from django.test import override_settings
from django.urls import path


def _controlled_failure(request):
    raise RuntimeError("controlled server exception")


urlpatterns = [path("controlled-error/", _controlled_failure)]


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
def test_production_500_logs_traceback_without_exposing_debug_page_or_post_body(
    client, monkeypatch
):
    output = io.StringIO()
    for handler in logging.getLogger("django.request").handlers:
        monkeypatch.setattr(handler, "stream", output)
    client.raise_request_exception = False
    response = client.post("/controlled-error/", {"password": "private-post-field-marker"})

    stderr = output.getvalue()
    assert response.status_code == 500
    assert "django.request: Internal Server Error: /controlled-error/" in stderr
    assert "Traceback (most recent call last)" in stderr
    assert "RuntimeError: controlled server exception" in stderr
    assert "private-post-field-marker" not in stderr
    assert b"controlled server exception" not in response.content
    assert b"Traceback" not in response.content
