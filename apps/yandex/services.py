import hashlib
import json
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.metrics.models import MetricPoint, SourceSnapshot

from .client import (
    CORE_METRICS,
    GOAL_CONVERSION_RATE,
    GOAL_REACHES,
    LAST_SIGN_TRAFFIC_SOURCE,
    MetrikaClient,
)
from .models import YandexMetrikaSyncRun

METRIC_CODES = (
    "visits",
    "users",
    "new_users",
    "bounce_rate",
    "page_depth",
    "avg_visit_duration_seconds",
)
METRIC_UNITS = ("count", "count", "count", "percent", "number", "seconds")
TRAFFIC_SOURCE_CODES = {
    "organic": "search",
    "direct": "direct",
    "referral": "referral",
    "ad": "advertising",
    "social": "social",
    "internal": "internal",
    "recommend": "other",
    "messenger": "other",
    "saved": "other",
    "undefined": "other",
}


def shift_month(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def _value(row, index):
    values = row.get("metrics", [])
    return Decimal(str(values[index] if index < len(values) and values[index] is not None else 0))


def _fetch_month(client, mapping, month):
    common = {
        "ids": mapping.counter_id,
        "date1": month.isoformat(),
        "date2": month_end(month).isoformat(),
        "accuracy": "full",
        "attribution": "lastsign",
    }
    totals = client.stat(**common, metrics=",".join(CORE_METRICS))
    row = (totals.get("data") or [{"metrics": totals.get("totals", [])}])[0]
    points = [
        {"code": code, "value": str(_value(row, i)), "unit": unit, "dimensions": {}}
        for i, (code, unit) in enumerate(zip(METRIC_CODES, METRIC_UNITS, strict=True))
    ]
    sources = client.stat(
        **common, metrics="ym:s:visits", dimensions=LAST_SIGN_TRAFFIC_SOURCE, limit=10000
    )
    cleaned_sources = []
    aggregated = {}
    for item in sources.get("data", []):
        dim = (item.get("dimensions") or [{}])[0]
        source_id, source_name = str(dim.get("id", "undefined")), str(dim.get("name", ""))
        normalized = TRAFFIC_SOURCE_CODES.get(source_id, "other")
        value = _value(item, 0)
        aggregated[normalized] = aggregated.get(normalized, Decimal(0)) + value
        cleaned_sources.append(
            {"id": source_id, "name": source_name, "code": normalized, "visits": str(value)}
        )
    for code in ("search", "direct", "referral", "advertising", "social", "internal", "other"):
        points.append(
            {
                "code": f"source_{code}_visits",
                "value": str(aggregated.get(code, 0)),
                "unit": "count",
                "dimensions": {},
            }
        )
    cleaned_goals = []
    for goal in mapping.selected_goals:
        goal_id = str(goal["id"])
        response = client.stat(
            **common,
            metrics=f"{GOAL_REACHES.format(id=goal_id)},{GOAL_CONVERSION_RATE.format(id=goal_id)}",
        )
        goal_row = (response.get("data") or [{"metrics": response.get("totals", [])}])[0]
        dimensions = {
            "goal_id": goal_id,
            "name": goal.get("name", ""),
            "label": goal.get("label", goal.get("name", "")),
        }
        points.extend(
            (
                {
                    "code": f"goal_{goal_id}_reaches",
                    "value": str(_value(goal_row, 0)),
                    "unit": "count",
                    "dimensions": dimensions,
                },
                {
                    "code": f"goal_{goal_id}_conversion_rate",
                    "value": str(_value(goal_row, 1)),
                    "unit": "percent",
                    "dimensions": dimensions,
                },
            )
        )
        cleaned_goals.append(
            {**dimensions, "reaches": points[-2]["value"], "conversion_rate": points[-1]["value"]}
        )
    return {
        "period_start": month.isoformat(),
        "period_end": month_end(month).isoformat(),
        "metrics": points,
        "traffic_sources": cleaned_sources,
        "goals": cleaned_goals,
        "sampled": bool(totals.get("sampled")),
        "sample_share": totals.get("sample_share"),
    }


def sync_metrika(*, mapping, report_month, user=None, client=None):
    run = YandexMetrikaSyncRun.objects.create(
        mapping=mapping, report_month=report_month.replace(day=1)
    )
    client = client or MetrikaClient(mapping.connection)
    try:
        fetched = [
            _fetch_month(client, mapping, shift_month(report_month.replace(day=1), offset))
            for offset in (-2, -1, 0)
        ]
        now = timezone.now()
        with transaction.atomic():
            for data in fetched:
                payload = {
                    "schema_version": 1,
                    "source": "yandex_metrika",
                    "retrieval_method": "yandex_api",
                    "counter_id": mapping.counter_id,
                    **data,
                    "retrieved_at": now.isoformat(),
                    "contains_sensitive_data": False,
                }
                checksum = hashlib.sha256(
                    json.dumps(
                        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                snapshot, _ = SourceSnapshot.objects.update_or_create(
                    project=mapping.project,
                    source=SourceSnapshot.Source.METRIKA,
                    period_start=date.fromisoformat(data["period_start"]),
                    period_end=date.fromisoformat(data["period_end"]),
                    defaults={
                        "retrieval_method": SourceSnapshot.RetrievalMethod.YANDEX_API,
                        "payload": payload,
                        "checksum": checksum,
                        "generated_by": user,
                        "retrieved_at": now,
                        "provenance": {
                            "method": "yandex_api",
                            "counter_id": mapping.counter_id,
                            "period": f"{data['period_start']}/{data['period_end']}",
                            "retrieved_at": now.isoformat(),
                        },
                        "sampling": {"sampled": data["sampled"], "share": data["sample_share"]},
                        "contains_sensitive_data": False,
                    },
                )
                snapshot.metrics.all().delete()
                MetricPoint.objects.bulk_create(
                    [
                        MetricPoint(
                            snapshot=snapshot,
                            metric_code=p["code"],
                            numeric_value=Decimal(p["value"]),
                            unit=p["unit"],
                            dimensions=p["dimensions"],
                        )
                        for p in data["metrics"]
                    ]
                )
            mapping.last_successful_sync_at = now
            mapping.save(update_fields=["last_successful_sync_at", "updated_at"])
            run.status = run.Status.SUCCESS
            run.completed_at = now
            run.save(update_fields=["status", "completed_at"])
        return run
    except Exception:
        run.status = run.Status.FAILED
        run.completed_at = timezone.now()
        run.error_message = "Не удалось синхронизировать данные Метрики."
        run.save(update_fields=["status", "completed_at", "error_message"])
        return run
