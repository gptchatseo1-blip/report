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


@pytest.mark.parametrize(
    ("label", "absolute", "expected"),
    [
        ("Видимость", "1", "Видимость выросла"),
        ("Видимость", "-1", "Видимость снизилась"),
        ("Клики", "1", "Клики выросли"),
        ("Показы", "-1", "Показы снизились"),
        (
            "Количество проиндексированных страниц",
            "1",
            "Количество проиндексированных страниц выросло",
        ),
    ],
)
def test_change_templates_use_correct_russian_grammar(label, absolute, expected):
    text = _change_text(
        label,
        {
            "current": "101",
            "previous": "100",
            "absolute": absolute,
            "relative_percent": "1",
            "percentage_points": None,
        },
    )
    assert expected in text


def test_missing_previous_period_and_missing_data_are_explicit():
    version = make_version(previous=False)
    texts = {block.section_code: block.generated_text for block in version.narrative_blocks.all()}
    assert "предыдущ" in texts["position_dynamics"].lower()
    assert texts["traffic"].endswith("Данные раздела отсутствуют.")
    assert "месячными итогами" in texts["traffic"]


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


def _position_payload_with_segments(version, variants):
    payload = version.snapshot.payload
    original = payload["calculated"]["positions"]["segments"][0]
    segments = []
    for engine, region, depth, query, position in variants:
        segment = {**original}
        segment["search_engine"] = engine
        segment["region"] = region
        segment["ranking_depth"] = depth
        segment["depth_comment"] = None
        segment["distribution"] = {
            "total": 1,
            "ranges": {"1-3": 0, "4-10": 0, **({"11-20": 1} if depth >= 20 else {})},
            "top_10": 0,
            "top_30": 1 if depth >= 30 else None,
        }
        segment["top_11_20"] = (
            [{"query": query, "frequency": 100, "position": position}] if depth >= 20 else []
        )
        segments.append(segment)
    payload["calculated"]["positions"]["segments"] = segments
    return payload


def test_position_narratives_label_engines_regions_and_do_not_mix_top_11_20():
    from apps.reports.narratives import build_narrative_specs

    version = make_version()
    payload = _position_payload_with_segments(
        version,
        (
            ("google", "Москва", 20, "google query", 12),
            ("yandex", "Москва", 20, "yandex query", 15),
            ("google", "Санкт-Петербург", 20, "spb query", 18),
        ),
    )
    blocks = {item["section"]: item["text"] for item in build_narrative_specs(payload)}
    for section in ("visibility", "position_distribution", "top_10", "position_dynamics"):
        assert "Google, Москва:" in blocks[section]
        assert "Яндекс, Москва:" in blocks[section]
        assert "Google, Санкт-Петербург:" in blocks[section]
    top = blocks["top_11_20"]
    assert "Google, Москва: в диапазоне TOP-11–20 находятся запросы: google query (12)." in top
    assert "Яндекс, Москва: в диапазоне TOP-11–20 находятся запросы: yandex query (15)." in top
    assert (
        "Google, Санкт-Петербург: в диапазоне TOP-11–20 находятся запросы: spb query (18)." in top
    )


def test_different_google_depths_form_one_aggregate_safe_comment():
    from apps.reports.narratives import build_narrative_specs

    version = make_version(depth=20)
    payload = _position_payload_with_segments(
        version,
        (("google", "Москва", 10, "first", 10), ("google", "Россия", 20, "second", 12)),
    )
    text = next(
        item["text"]
        for item in build_narrative_specs(payload)
        if item["section"] == "position_distribution"
    )
    assert text.count("Глубина проверки позиций в Google") == 1
    assert "Google, Москва — до TOP-10" in text
    assert "Google, Россия — до TOP-20" in text
    assert "TOP-30" not in text


def test_missing_frequency_is_error_and_blocks_publication_without_prior_validation():
    version = make_version()
    payload = version.snapshot.payload
    payload["ranking_sources"][0]["positions"][0]["frequency"] = None
    ReportDatasetSnapshot.objects.filter(pk=version.snapshot.pk).update(
        payload=payload, checksum=snapshot_checksum(payload)
    )
    assert not version.validation_issues.exists()
    readiness = get_publication_readiness(version)
    issue = version.validation_issues.get(code="frequency_missing")
    assert issue.severity == "error"
    assert readiness.has_errors and not readiness.can_publish


def test_invalid_traffic_share_sum_is_error():
    version = make_version()
    payload = version.snapshot.payload
    payload["calculated"]["sources"]["sources"]["yandex_metrika"] = {
        "traffic_sources": {
            "total": "100",
            "shares": {"search": "40", "direct": "40"},
            "warning": None,
        }
    }
    ReportDatasetSnapshot.objects.filter(pk=version.snapshot.pk).update(
        payload=payload, checksum=snapshot_checksum(payload)
    )
    issue = next(
        issue
        for issue in validate_report_version(version)
        if issue.code == "traffic_shares_arithmetic"
    )
    assert issue.severity == "error" and issue.details["shares_sum"] == "80"


def test_generated_narrative_fields_are_immutable_and_edit_survives_regeneration():
    from django.core.exceptions import ValidationError

    block = make_version().narrative_blocks.get(section_code="top_10")
    original_text, original_facts = block.generated_text, block.facts
    block.generated_text = "Подмена"
    block.facts = {"value": 999}
    with pytest.raises(ValidationError):
        block.save()
    block.refresh_from_db()
    block.edited_text = "Пользовательский текст"
    block.save()
    generate_narratives(block.report_version)
    block.refresh_from_db()
    assert block.generated_text == original_text
    assert block.facts == original_facts
    assert block.edited_text == "Пользовательский текст"


def test_admin_sets_and_clears_confirmation_metadata(django_user_model):
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from apps.reports.admin import NarrativeBlockAdmin

    user = django_user_model.objects.create_user(username="editor", password="secret")
    block = make_version().narrative_blocks.get(section_code="top_10")
    request = RequestFactory().post("/admin/")
    request.user = user
    model_admin = NarrativeBlockAdmin(NarrativeBlock, AdminSite())

    block.status = NarrativeBlock.Status.CONFIRMED
    model_admin.save_model(request, block, form=None, change=True)
    block.refresh_from_db()
    assert block.confirmed_by == user and block.confirmed_at is not None

    block.status = NarrativeBlock.Status.EDITED
    model_admin.save_model(request, block, form=None, change=True)
    block.refresh_from_db()
    assert block.confirmed_by is None and block.confirmed_at is None
