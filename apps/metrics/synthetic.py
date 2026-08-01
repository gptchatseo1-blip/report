import calendar
import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from .models import MetricPoint, SourceSnapshot


def shift_month(value: date, offset: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 + offset
    year, zero_based_month = divmod(absolute_month, 12)
    return date(year, zero_based_month + 1, 1)


def month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _number(project, month, code, minimum, maximum):
    key = f"{project.normalized_domain}:{month.isoformat()}:{code}".encode()
    digest = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
    return minimum + digest % (maximum - minimum + 1)


def _decimal(value, places="0.01"):
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _metrika_payload(project, month):
    visits = _number(project, month, "visits", 1200, 6200)
    users = min(visits, _number(project, month, "users", int(visits * 0.62), int(visits * 0.9)))
    new_users = _number(project, month, "new_users", int(users * 0.45), int(users * 0.78))
    source_weights = {
        "search": _number(project, month, "search_weight", 48, 68),
        "direct": _number(project, month, "direct_weight", 12, 24),
        "referral": _number(project, month, "referral_weight", 6, 15),
        "internal": _number(project, month, "internal_weight", 2, 8),
    }
    weight_sum = sum(source_weights.values())
    source_values = {
        name: round(visits * weight / weight_sum) for name, weight in source_weights.items()
    }
    source_values["search"] += visits - sum(source_values.values())
    metrics = [
        ("visits", visits, MetricPoint.Unit.COUNT),
        ("users", users, MetricPoint.Unit.COUNT),
        ("new_users", new_users, MetricPoint.Unit.COUNT),
        (
            "bounce_rate",
            _decimal(_number(project, month, "bounce", 900, 3300) / 100),
            MetricPoint.Unit.PERCENT,
        ),
        (
            "page_depth",
            _decimal(_number(project, month, "depth", 140, 420) / 100),
            MetricPoint.Unit.NUMBER,
        ),
        (
            "avg_visit_duration_seconds",
            _number(project, month, "duration", 70, 260),
            MetricPoint.Unit.SECONDS,
        ),
    ]
    metrics.extend(
        (f"source_{name}_visits", value, MetricPoint.Unit.COUNT)
        for name, value in source_values.items()
    )
    return metrics


def _webmaster_payload(project, month):
    impressions = _number(project, month, "impressions", 14000, 85000)
    clicks = _number(project, month, "clicks", int(impressions * 0.025), int(impressions * 0.11))
    indexed_pages = _number(project, month, "indexed_pages", 180, 2200)
    excluded_pages = _number(project, month, "excluded_pages", 5, max(8, indexed_pages // 5))
    return [
        ("search_impressions", impressions, MetricPoint.Unit.COUNT),
        ("search_clicks", clicks, MetricPoint.Unit.COUNT),
        ("search_ctr", _decimal(clicks / impressions * 100), MetricPoint.Unit.PERCENT),
        (
            "average_position",
            _decimal(_number(project, month, "position", 450, 2850) / 100),
            MetricPoint.Unit.NUMBER,
        ),
        ("indexed_pages", indexed_pages, MetricPoint.Unit.COUNT),
        ("excluded_pages", excluded_pages, MetricPoint.Unit.COUNT),
        (
            "quality_index",
            _number(project, month, "quality_index", 40, 750),
            MetricPoint.Unit.COUNT,
        ),
    ]


BUILDERS = {
    SourceSnapshot.Source.METRIKA: _metrika_payload,
    SourceSnapshot.Source.WEBMASTER: _webmaster_payload,
}


def build_synthetic_payload(project, source, month):
    metrics = BUILDERS[source](project, month)
    return {
        "schema_version": 1,
        "source": source,
        "retrieval_method": SourceSnapshot.RetrievalMethod.SYNTHETIC,
        "project_domain": project.normalized_domain,
        "period_start": month.isoformat(),
        "period_end": month_end(month).isoformat(),
        "metrics": [
            {"code": code, "value": str(value), "unit": unit} for code, value, unit in metrics
        ],
    }


@transaction.atomic
def sync_synthetic_metrics(*, project, report_month, user=None):
    report_month = report_month.replace(day=1)
    snapshots = []
    created_count = 0
    for month_offset in (-2, -1, 0):
        month = shift_month(report_month, month_offset)
        for source in BUILDERS:
            payload = build_synthetic_payload(project, source, month)
            serialized = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            checksum = hashlib.sha256(serialized.encode()).hexdigest()
            snapshot, created = SourceSnapshot.objects.get_or_create(
                project=project,
                source=source,
                period_start=month,
                period_end=month_end(month),
                defaults={
                    "retrieval_method": SourceSnapshot.RetrievalMethod.SYNTHETIC,
                    "payload": payload,
                    "checksum": checksum,
                    "generated_by": user,
                },
            )
            if created:
                MetricPoint.objects.bulk_create(
                    [
                        MetricPoint(
                            snapshot=snapshot,
                            metric_code=item["code"],
                            numeric_value=Decimal(item["value"]),
                            unit=item["unit"],
                        )
                        for item in payload["metrics"]
                    ],
                    batch_size=1000,
                )
                created_count += 1
            snapshots.append(snapshot)
    return snapshots, created_count
