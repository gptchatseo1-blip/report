from datetime import date
from decimal import Decimal

import pytest

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports import views
from apps.reports.runtime_fixes_round7 import provider_visibility
from apps.topvisor.models import TopvisorProjectMapping

pytestmark = pytest.mark.django_db


def _project():
    project = Project.objects.create(
        name="Topvisor visibility precision",
        domain="topvisor-precision.example",
        position_provider=Project.PositionProvider.TOPVISOR,
    )
    TopvisorProjectMapping.objects.create(
        project=project,
        topvisor_project_id="42",
        selected_configurations=[
            {
                "id": "yandex-main",
                "search_engine": "yandex",
                "region_index": 1,
                "region_name": "Москва",
                "depth": 100,
            }
        ],
    )
    return project


def _snapshot(project):
    return RankingSnapshot.objects.create(
        project=project,
        snapshot_date=date(2026, 8, 31),
        search_engine="yandex",
        region="Москва",
        ranking_depth=100,
        depth_source=RankingSnapshot.DepthSource.TOPVISOR_API,
        topvisor_configuration_id="yandex-main",
        visibility=Decimal("15.0000"),
        visibility_raw={
            "value": "15",
            "source": "topvisor_api_summary_chart",
        },
        provenance={
            "visibility": {
                "value": "15",
                "source": "topvisor_api_summary_chart",
            }
        },
        response_checksum="precision-test",
    )


def _position(snapshot, query, frequency, position):
    return KeywordPosition.objects.create(
        ranking_snapshot=snapshot,
        query=query,
        normalized_query=query.casefold(),
        frequency=frequency,
        position_raw=str(position),
        position_value=position,
        position_status=KeywordPosition.Status.RANKED,
    )


def test_whole_provider_coordinate_recovers_fractional_topvisor_precision():
    project = _project()
    snapshot = _snapshot(project)
    _position(snapshot, "high", 1565, 1)
    _position(snapshot, "outside", 8435, 21)

    assert provider_visibility(snapshot) == Decimal("15.6500")


def test_editor_rounds_recovered_15_65_to_16_percent():
    project = _project()
    snapshot = _snapshot(project)
    _position(snapshot, "high", 1565, 1)
    _position(snapshot, "outside", 8435, 21)

    rows, _segments = views._topvisor_editor_data(project)

    august = next(row for row in rows if str(row.get("month", "")).startswith("2026-08"))
    assert august["visibility"] == 16.0


def test_fractional_provider_value_remains_authoritative():
    project = _project()
    snapshot = _snapshot(project)
    snapshot.visibility = Decimal("15.6500")
    snapshot.visibility_raw = {
        "value": "15.65",
        "source": "topvisor_api_summary_chart",
    }
    snapshot.save(update_fields=["visibility", "visibility_raw"])
    _position(snapshot, "different", 1500, 1)
    _position(snapshot, "outside", 8500, 21)

    assert provider_visibility(snapshot) == Decimal("15.65")


def test_unrelated_integer_provider_value_is_not_replaced_by_local_calculation():
    project = _project()
    snapshot = _snapshot(project)
    _position(snapshot, "high", 1490, 1)
    _position(snapshot, "outside", 8510, 21)

    assert provider_visibility(snapshot) == Decimal("15")
