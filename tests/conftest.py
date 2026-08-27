import pytest


@pytest.fixture(autouse=True)
def disable_external_favicon_fetch(settings):
    settings.REPORT_FAVICON_FETCH_ENABLED = False
