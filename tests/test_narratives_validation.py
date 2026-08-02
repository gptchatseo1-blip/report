from datetime import date

import pytest

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.projects.models import Project
from apps.reports.models import NarrativeBlock, Report, ReportDatasetSnapshot
from apps.reports.narratives import _change_text, generate_narratives
from apps.reports.services import create_report_version, snapshot_checksum
from apps.reports.validation import get_publication_readiness, validate_report_version

pytestmark = pytest.mark.django_db


def make_version(*, depth=30, previous=True):
    project = Project.objects.create(name=f"Site {depth}", domain=f"site-{depth}.example.com")
    months = (
        ((date(2026, 6, 30), 5), (date(2026, 7, 31), 12))
        if previous
        else ((date(2026, 7, 31), 12),)
    )
    for month, position in months:
        ranking = RankingSnapshot.objects.create(
            project=project,
            snapshot_date=month,
            search_engine="google",
            region="Россия",
            ranking_depth=depth,
            depth_raw=f"TOP-{depth}",
            tracked_keyword_count=1,
        )
        KeywordPosition.objects.create(
            ranking_snapshot=ranking,
            query="seo",
            normalized_query="seo",
            frequency=100,
            position_raw=str(position),
            position_value=position,
            position_status="ranked",
            normalized_target_url=f"https://{project.domain}/seo/",
        )
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    return create_report_version(report=report)


@pytest.mark.parametrize("depth", [10, 20, 30, 100])
def test_narratives_respect_checked_depth_and_emit_one_depth_comment(depth):
    version = make_version(depth=depth)
    blocks = {block.section_code: block for block in version.narrative_blocks.all()}
    distribution = blocks["position_distribution"].generated_text
    assert distribution.count("Проверка позиций в Google") == 1
    for unsupported in (30, 50, 100):
        if unsupported > depth:
            assert f"{unsupported}" not in distribution
    assert ("top_11_20" in blocks) is (depth >= 20)


def test_change_templates_distinguish_directions_zero_base_percent_and_points():
    increased = _change_text(
        "Трафик",
        {
            "current": "120",
            "previous": "100",
            "absolute": "20",
            "relative_percent": "20",
            "percentage_points": None,
        },
    )
    decreased = _change_text(
        "Трафик",
        {
            "current": "80",
            "previous": "100",
            "absolute": "-20",
            "relative_percent": "-20",
            "percentage_points": None,
        },
    )
    unchanged = _change_text(
        "Трафик",
        {
            "current": "100",
            "previous": "100",
            "absolute": "0",
            "relative_percent": "0",
            "percentage_points": None,
        },
    )
    zero_base = _change_text(
        "Трафик",
        {
            "current": "10",
            "previous": "0",
            "absolute": "10",
            "relative_percent": None,
            "percentage_points": None,
        },
    )
    points = _change_text(
        "CTR",
        {
            "current": "7",
            "previous": "5",
            "absolute": "2",
            "relative_percent": "40",
            "percentage_points": "2",
        },
    )
    assert "вырос" in increased and "относительное изменение — 20%" in increased
    assert "снизился" in decreased
    assert "существенного изменения нет" in unchanged
    assert "нулевой базы" in zero_base
    assert "2 процентного пункта" in points and "40%" not in points


def test_missing_previous_period_and_missing_data_are_explicit():
    version = make_version(previous=False)
    texts = {block.section_code: block.generated_text for block in version.narrative_blocks.all()}
    assert "предыдущ" in texts["position_dynamics"].lower()
    assert texts["traffic"] == "Данные раздела отсутствуют."


def test_edit_preserves_generated_facts_and_validator_replaces_issues():
    version = make_version()
    block = version.narrative_blocks.get(section_code="top_10")
    generated, facts = block.generated_text, block.facts
    block.edited_text = "В TOP-10 находится 999 запросов. {{ comment }}"
    block.status = NarrativeBlock.Status.EDITED
    block.save()
    assert block.effective_text == block.edited_text

    first = validate_report_version(version)
    first_count = len(first)
    second = validate_report_version(version)
    assert len(second) == first_count == version.validation_issues.count()
    assert {issue.code for issue in second} >= {
        "narrative_unsupported_number",
        "narrative_placeholder",
    }
    block.refresh_from_db()
    assert block.generated_text == generated and block.facts == facts
    assert get_publication_readiness(version).has_errors
    assert not get_publication_readiness(version).can_publish


def test_validator_detects_arithmetic_ctr_domain_provenance_and_secret():
    version = make_version()
    snapshot = version.snapshot
    payload = snapshot.payload
    segment = payload["calculated"]["positions"]["segments"][0]
    segment["distribution"]["top_10"] = 99
    payload["ranking_sources"][0]["positions"][0]["target_url"] = "https://other.example/"
    payload["ranking_sources"][0]["provenance"] = {}
    payload["project"]["leak"] = "Authorization: Bearer secret-token-value"
    payload["calculated"]["sources"]["sources"].setdefault("yandex_webmaster", {})["ctr_check"] = {
        "reported": "120",
        "calculated": "10",
        "valid": False,
    }
    ReportDatasetSnapshot.objects.filter(pk=snapshot.pk).update(
        payload=payload, checksum=snapshot_checksum(payload)
    )
    codes = {issue.code for issue in validate_report_version(version)}
    assert codes >= {
        "top_10_mismatch",
        "ctr_out_of_range",
        "ctr_mismatch",
        "foreign_metric_domain",
        "provenance_missing",
        "secret_detected",
    }


def test_warning_allows_draft_and_services_read_only_snapshot(monkeypatch):
    version = make_version(previous=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("live source was accessed")

    monkeypatch.setattr("apps.reports.services.build_position_facts", forbidden)
    monkeypatch.setattr("apps.reports.services.build_source_facts", forbidden)
    generate_narratives(version)
    issues = validate_report_version(version)
    assert any(issue.severity == "warning" for issue in issues)
    assert not [
        (issue.section_code, issue.code, issue.details)
        for issue in issues
        if issue.severity == "error"
    ]
    readiness = get_publication_readiness(version)
    assert readiness.has_warnings and readiness.can_export_draft
