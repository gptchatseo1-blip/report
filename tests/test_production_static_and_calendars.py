import re
from datetime import date
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def _calendar_page(client):
    user = get_user_model().objects.create_user("static-reviewer")
    project = Project.objects.create(name="Static review", domain="example.test")
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        selected_configurations=[{"id": "google-main", "search_engine": "google"}],
    )
    for day in (date(2026, 5, 3), date(2026, 7, 14)):
        RankingSnapshot.objects.create(
            project=project,
            snapshot_date=day,
            search_engine="google",
            topvisor_configuration_id="google-main",
            response_checksum=str(day),
        )
    client.force_login(user)
    return client.get(reverse("reports:report-list", args=[project.id]))


def test_template_uses_manifest_static_url_and_assets_are_served(client):
    response = _calendar_page(client)
    html = response.content.decode()
    script_url = staticfiles_storage.url("reports/calendar.js")
    css_url = staticfiles_storage.url("reports/app.css")
    favicon_url = staticfiles_storage.url("reports/favicon.png")
    assert script_url in html and css_url in html and favicon_url in html
    assert 'src="/static/reports/calendar.js"' not in html

    with override_settings(DEBUG=False):
        css = client.get(css_url)
        javascript = client.get(script_url)
    assert css.status_code == javascript.status_code == 200
    assert css.headers["Content-Type"].startswith("text/css")
    assert javascript.headers["Content-Type"].startswith("text/javascript")
    assert b".date-calendar" in b"".join(css.streaming_content)


def test_collectstatic_builds_manifest_from_empty_root(tmp_path, settings):
    static_root = tmp_path / "empty-static-root"
    settings.STATIC_ROOT = static_root
    call_command("collectstatic", interactive=False, verbosity=0)
    manifest = (static_root / "staticfiles.json").read_text()
    assert '"reports/app.css": "reports/app.' in manifest
    assert '"reports/calendar.js": "reports/calendar.' in manifest
    assert '"reports/favicon.png": "reports/favicon.' in manifest
    assert '"reports/source-picker.css": "reports/source-picker.' in manifest
    assert '"reports/source-picker.js": "reports/source-picker.' in manifest


def test_server_html_contains_three_months_dates_and_disabled_days(client):
    html = _calendar_page(client).content.decode()
    assert html.count('class="calendar-month"') == 3
    assert "Май 2026" in html and "Июнь 2026" in html and "Июль 2026" in html
    assert re.search(r"data-period[^>]*>Май — Июль 2026</span>", html)
    assert re.search(r'data-date="2026-07-14"[^>]*aria-pressed="false"', html)
    assert re.search(r'data-date="2026-07-13"[^>]*disabled', html)
    assert "Добавить релевантные URL в предпросмотр и файлы отчёта." in html
    checkbox = re.search(r'<input[^>]*id="id_show_urls"[^>]*>', html).group()
    assert 'type="checkbox"' in checkbox and "checked" not in checkbox
    assert "Яндекс.Метрика и Вебмастер" in html
    assert html.count('href="/yandex/projects/') == 1
    calendars_end = html.rindex("</section>", 0, html.index("Параметры отчёта"))
    options = html.index("Параметры отчёта")
    metrika = html.index("Яндекс.Метрика", options)
    assert calendars_end < options < metrika


def test_responsive_css_keeps_one_month_on_mobile():
    css = Path(staticfiles_storage.path("reports/app.css")).read_text()
    assert "grid-template-columns:repeat(3" in css
    assert ".calendar-month:not(:first-child){display:none}" in css
    assert ".calendar-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "@media(max-width:700px){.calendar-pair{grid-template-columns:1fr}" in css


def test_javascript_updates_period_during_every_render():
    javascript = Path(staticfiles_storage.path("reports/calendar.js")).read_text()
    render_body = javascript.split("function render()", 1)[1].split("root.querySelector", 1)[0]
    assert "period.textContent" in render_body
    assert "render();" in javascript


def test_source_picker_only_requires_confirmation_for_another_domain():
    javascript = Path(staticfiles_storage.path("reports/source-picker.js")).read_text()
    assert 'option.dataset.domainMismatch === "true"' in javascript
    assert "confirmation.hidden = !mismatch" in javascript
    assert "checkbox.required = mismatch" in javascript


def test_calendar_pair_keeps_yandex_first_and_engines_independent(client):
    user = get_user_model().objects.create_user("calendar-pair")
    project = Project.objects.create(name="Pair", domain="pair.example")
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="43",
        selected_configurations=[
            {"id": "google-main", "search_engine": "google"},
            {"id": "yandex-main", "search_engine": "yandex"},
        ],
    )
    for engine in ("google", "yandex"):
        for day in (date(2026, 7, 1), date(2026, 7, 31)):
            RankingSnapshot.objects.create(
                project=project,
                snapshot_date=day,
                search_engine=engine,
                topvisor_configuration_id=f"{engine}-main",
                response_checksum=f"{engine}-{day}",
            )
    client.force_login(user)
    html = client.get(reverse("reports:report-list", args=[project.id])).content.decode()
    pair = html[html.index('<div class="calendar-pair">') : html.index("Параметры отчёта")]
    assert pair.index('data-engine="yandex"') < pair.index('data-engine="google"')
    javascript = Path(staticfiles_storage.path("reports/calendar.js")).read_text()
    assert "document.querySelectorAll('[data-calendar]').forEach(root =>" in javascript
    assert "root.querySelectorAll('.calendar-source input[type=checkbox]')" in javascript
