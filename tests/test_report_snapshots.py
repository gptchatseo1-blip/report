from datetime import date

import pytest
from django.core.exceptions import ValidationError

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.models import Report
from apps.reports.services import (
    create_report_version,
    get_report_version_data,
    snapshot_checksum,
)

pytestmark = pytest.mark.django_db


def add_ranking(project, month, depth, position=12):
    snapshot = RankingSnapshot.objects.create(
        project=project,
        snapshot_date=month,
        search_engine="google",
        region="Россия",
        ranking_depth=depth,
        depth_raw=f"top-{depth}",
        tracked_keyword_count=1,
    )
    KeywordPosition.objects.create(
        ranking_snapshot=snapshot,
        query="SEO отчёт",
        normalized_query="seo отчёт",
        frequency=100,
        position_raw=str(position),
        position_value=position,
        position_status=KeywordPosition.Status.RANKED,
    )


def test_first_and_next_versions_are_explicit_and_snapshots_are_immutable():
    project = Project.objects.create(name="Demo", domain="example.com")
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    assert not report.versions.exists()

    first = create_report_version(report=report)
    project.name = "Changed"
    project.save()
    second = create_report_version(report=report)

    assert (first.number, second.number) == (1, 2)
    assert first.snapshot.payload["project"]["name"] == "Demo"
    assert second.snapshot.payload["project"]["name"] == "Changed"
    first.snapshot.payload = {}
    with pytest.raises(ValidationError, match="нельзя изменять"):
        first.snapshot.save()


def test_checksum_is_canonical_and_version_reads_only_snapshot(monkeypatch):
    project = Project.objects.create(name="Demo", domain="example.com")
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    version = create_report_version(report=report)
    assert snapshot_checksum({"b": 1, "a": [2]}) == snapshot_checksum({"a": [2], "b": 1})
    assert snapshot_checksum(version.snapshot.payload) == version.snapshot.checksum

    def unexpected_call(*args, **kwargs):
        raise AssertionError("source calculation must not run while reading a version")

    monkeypatch.setattr("apps.reports.services.build_position_facts", unexpected_call)
    monkeypatch.setattr("apps.reports.services.build_source_facts", unexpected_call)
    assert get_report_version_data(version) == version.snapshot.payload


@pytest.mark.parametrize(
    ("depth", "ranges", "top_30"),
    [
        (10, {"1-3", "4-10"}, None),
        (20, {"1-3", "4-10", "11-20"}, None),
        (30, {"1-3", "4-10", "11-20", "21-30"}, 1),
    ],
)
def test_google_depth_never_creates_unconfirmed_ranges(depth, ranges, top_30):
    project = Project.objects.create(name=f"Depth {depth}", domain=f"depth-{depth}.example.com")
    add_ranking(project, date(2026, 7, 31), depth)
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    segment = create_report_version(report=report).snapshot.payload["calculated"]["positions"][
        "segments"
    ][0]
    assert set(segment["distribution"]["ranges"]) == ranges
    assert segment["distribution"]["top_30"] == top_30
    assert bool(segment["top_11_20"]) is (depth >= 20)


def test_depth_change_issue_and_project_isolation():
    first_project = Project.objects.create(name="First", domain="first.example.com")
    second_project = Project.objects.create(name="Second", domain="second.example.com")
    add_ranking(first_project, date(2026, 6, 30), 10)
    add_ranking(first_project, date(2026, 7, 31), 20)
    add_ranking(second_project, date(2026, 7, 31), 100, position=1)
    report = Report.objects.create(project=first_project, report_month=date(2026, 7, 1))
    version = create_report_version(report=report)

    assert version.validation_issues.get().code == "ranking_depth_changed"
    ranking_sources = version.snapshot.payload["ranking_sources"]
    assert {item["id"] for item in ranking_sources} == {
        str(item.id) for item in first_project.ranking_snapshots.all()
    }
