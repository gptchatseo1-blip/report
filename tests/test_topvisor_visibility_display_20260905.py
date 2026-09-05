from datetime import date
from decimal import Decimal

import pytest

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project
from apps.reports import exporting, services, views
from apps.reports.runtime_fixes_round3 import topvisor_display_visibility

pytestmark = pytest.mark.django_db


def _snapshot(project, day, visibility):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=day,
        search_engine="yandex",
        region="Москва",
        topvisor_configuration_id="yandex-main",
        ranking_depth=100,
        visibility=visibility,
        response_checksum=f"{day}-{visibility}",
    )


def test_topvisor_display_visibility_does_not_round_up():
    assert topvisor_display_visibility("15.65") == Decimal("15")
    assert topvisor_display_visibility("15.99") == Decimal("15")
    assert topvisor_display_visibility("15.00") == Decimal("15")
    assert topvisor_display_visibility("0.99") == Decimal("0")


def test_position_facts_use_topvisor_integer_visibility():
    project = Project.objects.create(
        name="Topvisor visibility",
        domain="visibility.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    _snapshot(project, date(2026, 7, 31), "14.90")
    _snapshot(project, date(2026, 8, 31), "15.65")

    facts = services.build_position_facts(project=project, report_month=date(2026, 8, 1))
    segment = facts["segments"][0]

    assert [point["visibility"] for point in segment["three_month_series"]] == [
        Decimal("14"),
        Decimal("15"),
    ]
    assert segment["visibility_change"].previous == Decimal("14")
    assert segment["visibility_change"].current == Decimal("15")
    assert segment["visibility_change"].percentage_points == Decimal("1")


def test_topvisor_editor_shows_same_integer_as_provider():
    project = Project.objects.create(
        name="Topvisor editor",
        domain="editor-visibility.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    _snapshot(project, date(2026, 8, 31), "15.65")

    rows, _segments = views._topvisor_editor_data(project)

    assert rows[0]["visibility"] == 15.0


def test_existing_topvisor_report_segment_is_normalized_at_export_time():
    payload = {
        "project": {"position_provider": "topvisor"},
        "display_options": {"topvisor_manual_rows": []},
    }
    segment = {
        "configuration_id": "yandex-main",
        "search_engine": "yandex",
        "region": "Москва",
        "ranking_depth": 100,
        "three_month_series": [
            {"month": "2026-08-01", "visibility": Decimal("15.65"), "distribution": {}},
        ],
        "chart_series": [
            {"month": "2026-08-31", "visibility": Decimal("15.65"), "distribution": {}},
        ],
    }

    effective = exporting._manual_topvisor_segment(payload, segment)

    assert effective["three_month_series"][0]["visibility"] == Decimal("15")
    assert effective["chart_series"][0]["visibility"] == Decimal("15")


def test_manual_visibility_override_keeps_explicit_decimal_value():
    payload = {
        "project": {"position_provider": "topvisor"},
        "display_options": {
            "topvisor_manual_rows": [
                {
                    "configuration_id": "yandex-main",
                    "engine": "yandex",
                    "region": "Москва",
                    "month": "2026-08-01",
                    "visibility": 13.5,
                    "total": 100,
                    "top3": 10,
                    "top10": 30,
                    "top11_30": 20,
                    "top3_percent": 10,
                    "top10_percent": 30,
                    "top11_30_percent": 20,
                }
            ]
        },
    }
    segment = {
        "configuration_id": "yandex-main",
        "search_engine": "yandex",
        "region": "Москва",
        "ranking_depth": 100,
        "three_month_series": [
            {"month": "2026-08-01", "visibility": Decimal("15.65"), "distribution": {}},
        ],
    }

    effective = exporting._manual_topvisor_segment(payload, segment)

    assert effective["three_month_series"][0]["visibility"] == 13.5


def test_serphunt_visibility_is_not_changed():
    payload = {
        "project": {"position_provider": "serphunt"},
        "display_options": {"topvisor_manual_rows": []},
    }
    segment = {
        "configuration_id": "serphunt-main",
        "search_engine": "yandex",
        "region": "Москва",
        "ranking_depth": 100,
        "three_month_series": [
            {"month": "2026-08-01", "visibility": Decimal("15.65"), "distribution": {}},
        ],
    }

    effective = exporting._manual_topvisor_segment(payload, segment)

    assert effective["three_month_series"][0]["visibility"] == Decimal("15.65")
