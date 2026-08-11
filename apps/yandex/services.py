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
    WebmasterClient,
)
from .models import YandexMetrikaSyncRun, YandexWebmasterSyncRun

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


def _dated_rows(response):
    """Return API history rows without inventing absent values."""
    if isinstance(response, list):
        return response
    for key in ("history", "indicators", "data", "points"):
        value = response.get(key)
        if isinstance(value, list):
            return value
    return []


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _sum_metric(rows, names):
    values = []
    for row in rows:
        for name in names:
            if name in row:
                value = _number(row[name])
                if value is not None:
                    values.append(value)
                break
    return sum(values, Decimal(0)) if values else None


def _response_values(response, names):
    """Read both v4.1 indicator-series and row-oriented test/provider responses."""
    indicators = response.get("indicators", {}) if isinstance(response, dict) else {}
    if isinstance(indicators, dict):
        for name in names:
            series = indicators.get(name)
            if isinstance(series, list):
                values = [_number(item.get("value")) for item in series if isinstance(item, dict)]
                return [value for value in values if value is not None]
    rows = _dated_rows(response)
    values = []
    for row in rows:
        nested = row.get("value", {}) if isinstance(row.get("value"), dict) else row
        for name in names:
            if name in nested:
                value = _number(nested[name])
                if value is not None:
                    values.append(value)
                break
    return values


def _last_metric(rows, names):
    for row in reversed(rows):
        for name in names:
            if name in row:
                value = _number(row[name])
                if value is not None:
                    return value
    return None


def _webmaster_month(
    client, mapping, user_id, month, *, host=None, summary=None, include_current=False
):
    start, end = month, month_end(month)
    params = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    queries = client.search_query_history(
        user_id,
        mapping.host_id,
        **params,
        device_type_indicator="ALL",
        query_indicator=["TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION"],
    )
    pages = client.search_urls_history(user_id, mapping.host_id, **params)
    indexing = client.indexing_history(user_id, mapping.host_id, **params)
    sqi = client.squ_history(user_id, mapping.host_id, **params)
    impressions_values = _response_values(queries, ("TOTAL_SHOWS", "shows", "impressions"))
    click_values = _response_values(queries, ("TOTAL_CLICKS", "clicks"))
    position_values = _response_values(queries, ("AVG_SHOW_POSITION", "avg_show_position"))
    impressions = sum(impressions_values, Decimal(0)) if impressions_values else None
    clicks = sum(click_values, Decimal(0)) if click_values else None
    metrics = {}
    if impressions is not None:
        metrics["search_impressions"] = (impressions, "count")
    if clicks is not None:
        metrics["search_clicks"] = (clicks, "count")
    if impressions not in (None, 0) and clicks is not None:
        metrics["search_ctr"] = (clicks * Decimal(100) / impressions, "percent")
    if position_values:
        metrics["average_position"] = (
            sum(position_values, Decimal(0)) / len(position_values),
            "number",
        )
    aliases = {
        "indexed_pages": (pages, ("SEARCHABLE", "searchable_pages_count", "count")),
        "added_pages": (indexing, ("APPEARED_IN_SEARCH", "added", "appeared")),
        "excluded_pages": (indexing, ("REMOVED_FROM_SEARCH", "excluded", "removed")),
        "iks": (sqi, ("sqi", "value", "quality_index")),
    }
    for code, (response, names) in aliases.items():
        values = _response_values(response, names)
        value = (
            (values[-1] if values else None)
            if code in ("indexed_pages", "iks")
            else (sum(values, Decimal(0)) if values else None)
        )
        if value is not None:
            metrics[code] = (value, "count" if code != "iks" else "number")
    if include_current and summary:
        for code, names in {
            "searchable_pages_count": ("searchable_pages_count", "searchable_pages"),
            "excluded_pages_count": ("excluded_pages_count", "excluded_pages"),
        }.items():
            value = next((_number(summary.get(name)) for name in names if name in summary), None)
            if value is not None:
                metrics[code] = (value, "count")
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "metrics": metrics,
        "problems": (summary or {}).get("problems", []),
        "actual_period": queries.get("date_range", params) if isinstance(queries, dict) else params,
        "availability_reason": None if metrics else "API не вернул данные за период.",
        "raw": {"queries": queries, "pages": pages, "indexing": indexing, "sqi": sqi},
        "host": host or {},
    }


def sync_webmaster(*, mapping, report_month, user=None, client=None):
    """Fetch everything first, then atomically replace the three monthly snapshots."""
    month = report_month.replace(day=1)
    run = YandexWebmasterSyncRun.objects.create(mapping=mapping, report_month=month)
    client = client or WebmasterClient(mapping.connection)
    try:
        user_response = client.user()
        user_id = user_response.get("user_id") or user_response.get("id")
        if not user_id:
            raise ValueError("missing user id")
        allowed = {str(item.get("host_id")): item for item in client.hosts(user_id)}
        if mapping.host_id not in allowed:
            raise ValueError("host is no longer available")
        host = client.host(user_id, mapping.host_id)
        summary = client.summary(user_id, mapping.host_id)
        fetched = [
            _webmaster_month(
                client,
                mapping,
                user_id,
                shift_month(month, offset),
                host=host,
                summary=summary,
                include_current=offset == 0,
            )
            for offset in (-2, -1, 0)
        ]
        now = timezone.now()
        with transaction.atomic():
            for data in fetched:
                payload = {
                    "schema_version": 1,
                    "source": "yandex_webmaster",
                    "retrieval_method": "yandex_api",
                    "host_id": mapping.host_id,
                    "host_url": mapping.host_url,
                    "period_start": data["period_start"],
                    "period_end": data["period_end"],
                    "actual_period": data["actual_period"],
                    "retrieved_at": now.isoformat(),
                    "availability_reason": data["availability_reason"],
                    "problems": data["problems"],
                    "metrics": {key: str(value[0]) for key, value in data["metrics"].items()},
                    "contains_sensitive_data": False,
                }
                checksum = hashlib.sha256(
                    json.dumps(
                        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                snapshot, _ = SourceSnapshot.objects.update_or_create(
                    project=mapping.project,
                    source=SourceSnapshot.Source.WEBMASTER,
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
                            "resource": "webmaster_v4.1",
                            "host_id": mapping.host_id,
                            "actual_period": data["actual_period"],
                            "retrieved_at": now.isoformat(),
                        },
                        "sampling": {},
                        "contains_sensitive_data": False,
                    },
                )
                snapshot.metrics.all().delete()
                MetricPoint.objects.bulk_create(
                    [
                        MetricPoint(
                            snapshot=snapshot, metric_code=code, numeric_value=value, unit=unit
                        )
                        for code, (value, unit) in data["metrics"].items()
                    ]
                )
            mapping.last_successful_sync_at = now
            mapping.save(update_fields=["last_successful_sync_at", "updated_at"])
            run.status, run.completed_at = run.Status.SUCCESS, now
            run.save(update_fields=["status", "completed_at"])
        return run
    except Exception:
        run.status = run.Status.FAILED
        run.completed_at = timezone.now()
        run.error_message = "Не удалось синхронизировать данные Вебмастера. Повторите позже."
        run.save(update_fields=["status", "completed_at", "error_message"])
        return run
