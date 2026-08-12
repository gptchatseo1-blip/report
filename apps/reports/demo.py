"""Deterministic, offline fixture for the complete MVP-1 reporting flow."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from django.db import transaction

from apps.metrics.models import KeywordPosition, MetricPoint, RankingSnapshot, SourceSnapshot
from apps.metrics.synthetic import build_synthetic_payload, month_end
from apps.projects.models import Project, ProjectBrandRule, ProjectUrlGroup, ProjectUrlRule
from apps.worklog.models import WorkCategory, WorkLogItem

from .models import Report
from .services import build_report_snapshot, create_report_version, snapshot_checksum
from .validation import validate_report_version

DEMO_DOMAIN = "seo-demo.invalid"
DEMO_MONTH = date(2026, 7, 1)
MONTHS = (date(2026, 5, 1), date(2026, 6, 1), DEMO_MONTH)
ENGINES = (("google", 20), ("yandex", 100))
FIXED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _checksum(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _project_rules(project):
    for pattern, priority in (("ДемоМарка", 100), ("demo marka", 90)):
        ProjectBrandRule.objects.update_or_create(
            project=project,
            kind=ProjectBrandRule.Kind.LITERAL,
            pattern=pattern,
            defaults={"priority": priority, "active": True},
        )
    groups = (
        ("Приоритетные услуги", "priority-services", 100, "/services/"),
        ("Коммерческие страницы", "commercial", 90, "/services/priority/"),
    )
    for name, slug, priority, pattern in groups:
        group, _ = ProjectUrlGroup.objects.update_or_create(
            project=project,
            slug=slug,
            defaults={"name": name, "priority": priority, "active": True},
        )
        ProjectUrlRule.objects.update_or_create(
            group=group,
            type=ProjectUrlRule.Type.STARTS_WITH,
            pattern=f"https://{DEMO_DOMAIN}{pattern}",
            defaults={"priority": priority, "active": True},
        )


def _ranking_rows(engine, depth, month_index):
    rows = []
    # Includes TOP-11–20 and an explicit not-found item without inventing a position.
    positions = (2, 6, 12, 17, depth, None)
    for index, position in enumerate(positions, 1):
        query = f"обезличенный {engine} запрос {index}"
        target = (
            f"https://{DEMO_DOMAIN}/services/priority/{index}"
            if index <= 2
            else f"https://{DEMO_DOMAIN}/catalog/{index}"
        )
        current = min(position + 2 - month_index, depth) if position is not None else None
        rows.append(
            KeywordPosition(
                query=query,
                normalized_query=query,
                frequency=120 + index * 35,
                position_raw=str(current) if current is not None else f">{depth}",
                position_value=current,
                position_status=(
                    KeywordPosition.Status.RANKED
                    if current is not None
                    else KeywordPosition.Status.NOT_FOUND
                ),
                group_name="Приоритет",
                target_url=target,
                normalized_target_url=target,
            )
        )
    return rows


def _rankings(project):
    for month_index, month in enumerate(MONTHS):
        snapshot_date = month_end(month)
        for engine, depth in ENGINES:
            payload = {
                "schema_version": 1,
                "source": "demo_offline_fixture",
                "engine": engine,
                "region": "Россия",
                "date": snapshot_date.isoformat(),
                "depth": depth,
            }
            snapshot, _ = RankingSnapshot.objects.update_or_create(
                project=project,
                snapshot_date=snapshot_date,
                search_engine=engine,
                region="Россия",
                defaults={
                    "ranking_depth": depth,
                    "depth_raw": str(depth),
                    "depth_source": RankingSnapshot.DepthSource.MANUAL,
                    "depth_retrieved_at": FIXED_AT,
                    "visibility": Decimal("18.50") + month_index * 2 + (engine == "yandex"),
                    "tracked_keyword_count": 6,
                    "response_checksum": _checksum(payload),
                    "retrieved_at": FIXED_AT,
                    "provenance": {
                        "method": "demo_offline_fixture",
                        "checksum": _checksum(payload),
                        "ranking_depth": depth,
                    },
                },
            )
            snapshot.positions.all().delete()
            rows = _ranking_rows(engine, depth, month_index)
            for row in rows:
                row.ranking_snapshot = snapshot
            KeywordPosition.objects.bulk_create(rows)


def _sources(project):
    for month in MONTHS:
        for source in (SourceSnapshot.Source.METRIKA, SourceSnapshot.Source.WEBMASTER):
            payload = build_synthetic_payload(project, source, month)
            if source == SourceSnapshot.Source.METRIKA:
                payload["metrics"].extend(
                    (
                        {"code": "goal_request_conversions", "value": "37", "unit": "count"},
                        {"code": "goal_order_conversions", "value": "14", "unit": "count"},
                        {"code": "conversion_rate", "value": "2.75", "unit": "percent"},
                    )
                )
            checksum = _checksum(payload)
            snapshot, _ = SourceSnapshot.objects.update_or_create(
                project=project,
                source=source,
                period_start=month,
                period_end=month_end(month),
                defaults={
                    "retrieval_method": SourceSnapshot.RetrievalMethod.SYNTHETIC,
                    "payload": payload,
                    "checksum": checksum,
                    "provenance": {"method": "demo_offline_fixture", "checksum": checksum},
                    "sampling": {"sampled": False},
                    "contains_sensitive_data": False,
                },
            )
            snapshot.metrics.all().delete()
            MetricPoint.objects.bulk_create(
                [
                    MetricPoint(
                        snapshot=snapshot,
                        metric_code=item["code"],
                        numeric_value=Decimal(item["value"]),
                        unit=item["unit"],
                    )
                    for item in payload["metrics"]
                ]
            )


def _worklog(project):
    category, _ = WorkCategory.objects.update_or_create(
        project=project,
        slug="technical-seo",
        defaults={"name": "Техническая оптимизация", "sort_order": 10, "active": True},
    )
    WorkLogItem.objects.update_or_create(
        project=project,
        work_date=date(2026, 7, 15),
        category=category,
        title="Исправлена перелинковка приоритетных страниц",
        defaults={
            "status": WorkLogItem.Status.COMPLETED,
            "url": f"https://{DEMO_DOMAIN}/services/priority/",
            "page_or_material_name": "Приоритетные услуги",
            "responsible": "Команда SEO",
            "comment": "Обезличенный результат демонстрационной работы.",
        },
    )
    WorkLogItem.objects.filter(project=project).update(updated_at=FIXED_AT)


@transaction.atomic
def create_demo_project():
    """Create/update stable inputs and return one reusable frozen report version."""
    project, _ = Project.objects.update_or_create(
        normalized_domain=DEMO_DOMAIN,
        defaults={
            "name": "Обезличенный демонстрационный проект",
            "domain": DEMO_DOMAIN,
            "timezone": "Europe/Moscow",
            "language": "ru",
            "active": True,
            "top_11_20_mode": Project.Top1120Mode.ENABLED,
        },
    )
    Project.objects.filter(pk=project.pk).update(updated_at=FIXED_AT)
    project.refresh_from_db()
    _project_rules(project)
    _rankings(project)
    _sources(project)
    _worklog(project)
    report, _ = Report.objects.get_or_create(project=project, report_month=DEMO_MONTH)
    payload_checksum = snapshot_checksum(build_report_snapshot(report=report))
    latest = report.versions.select_related("snapshot").order_by("-number").first()
    version = latest if latest and latest.snapshot.checksum == payload_checksum else None
    if version is None:
        version = create_report_version(report=report)
    validate_report_version(version)
    return project, report, version
