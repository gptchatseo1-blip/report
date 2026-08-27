import hashlib
import json
import logging
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from apps.metrics.models import MetricPoint, SourceSnapshot

from .client import (
    CORE_METRICS,
    GOAL_CONVERSION_RATE,
    GOAL_REACHES,
    GOAL_VISITS,
    LAST_SIGN_TRAFFIC_SOURCE,
    REGION_AREA,
    REGION_CITY,
    MetrikaClient,
    WebmasterClient,
    YandexAPIError,
    YandexUnauthorized,
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
GEOGRAPHY_CODES = (
    "moscow",
    "saint_petersburg",
    "undefined",
    "area_undefined",
)
LAST_SIGN_SEARCH_ENGINE = "ym:s:lastSignSearchEngineRoot"
START_URL = "ym:s:startURL"
SEARCH_HUMANS_FILTER = "ym:s:lastSignTrafficSource=='organic' AND ym:s:isRobot=='No'"
SEARCH_ALL_FILTER = "ym:s:lastSignTrafficSource=='organic'"
HUMANS_FILTER = "ym:s:isRobot=='No'"
SEARCH_DETAIL_METRICS = "ym:s:visits,ym:s:users,ym:s:bounceRate"
logger = logging.getLogger(__name__)
OPTIONAL_WEBMASTER_CODES = {"HOST_NOT_INDEXED", "HOST_NOT_LOADED"}


def shift_month(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def _value(row, index):
    values = row.get("metrics", [])
    return Decimal(str(values[index] if index < len(values) and values[index] is not None else 0))


def _dimension_name(item, index):
    dimensions = item.get("dimensions") or []
    if index >= len(dimensions) or not isinstance(dimensions[index], dict):
        return ""
    return str(dimensions[index].get("name") or "").strip().casefold()


def _dimension(item, index):
    dimensions = item.get("dimensions") or []
    if index >= len(dimensions) or not isinstance(dimensions[index], dict):
        return {"id": "", "name": ""}
    value = dimensions[index]
    return {"id": str(value.get("id") or ""), "name": str(value.get("name") or "").strip()}


def _detail_rows(response, dimension_count):
    rows = []
    for item in response.get("data", []):
        rows.append(
            {
                "dimensions": [_dimension(item, index) for index in range(dimension_count)],
                "visits": str(_value(item, 0)),
                "users": str(_value(item, 1)),
                "bounce_rate": str(_value(item, 2)),
            }
        )
    return rows


def _stat_with_filter(client, filter_value, **params):
    if filter_value:
        params["filters"] = filter_value
    return client.stat(**params)


def _geography_totals(rows):
    totals = {code: Decimal(0) for code in GEOGRAPHY_CODES}
    for row in rows:
        normalized = {
            "dimensions": row.get("dimensions") or [],
            "metrics": [row.get("visits")],
        }
        code = _geography_code(normalized)
        if code:
            totals[code] += _value(normalized, 0)
    return totals


def _geography_code(item):
    area = _dimension_name(item, 0)
    city = _dimension_name(item, 1)
    if city in {"москва", "moscow"}:
        return "moscow"
    if city in {"санкт-петербург", "saint petersburg", "st. petersburg"}:
        return "saint_petersburg"
    undefined = {"", "не определено", "undefined", "not defined"}
    if city in undefined:
        return "undefined"
    if area in undefined | {"область не определена", "area not defined"}:
        return "area_undefined"
    return None


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
    traffic_by_segment = {"search": {}, "all": {}}
    for segment, robotness, filter_value in (
        ("search", "humans", SEARCH_HUMANS_FILTER),
        ("search", "all", SEARCH_ALL_FILTER),
        ("all", "humans", HUMANS_FILTER),
        ("all", "all", None),
    ):
        variant_row = row
        if filter_value is not None:
            response = _stat_with_filter(
                client, filter_value, **common, metrics=",".join(CORE_METRICS)
            )
            variant_row = (response.get("data") or [{"metrics": response.get("totals", [])}])[0]
        values = {code: str(_value(variant_row, index)) for index, code in enumerate(METRIC_CODES)}
        traffic_by_segment[segment][robotness] = values
        points.extend(
            {
                "code": f"segment_{segment}_{robotness}_{code}",
                "value": value,
                "unit": unit,
                "dimensions": {"segment": segment, "robotness": robotness},
            }
            for (code, value), unit in zip(values.items(), METRIC_UNITS, strict=True)
        )
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
    geography = client.stat(
        **common,
        metrics="ym:s:visits",
        dimensions=f"{REGION_AREA},{REGION_CITY}",
        limit=10000,
        lang="ru",
    )
    geography_totals = {code: Decimal(0) for code in GEOGRAPHY_CODES}
    geography_rows = []
    for item in geography.get("data", []):
        code = _geography_code(item)
        if code is None:
            continue
        value = _value(item, 0)
        geography_totals[code] += value
        geography_rows.append(
            {
                "code": code,
                "area": _dimension_name(item, 0),
                "city": _dimension_name(item, 1),
                "visits": str(value),
            }
        )
    for code in GEOGRAPHY_CODES:
        points.append(
            {
                "code": f"geography_{code}_visits",
                "value": str(geography_totals[code]),
                "unit": "count",
                "dimensions": {"region_code": code},
            }
        )

    detail_variants = {"search": {}, "all": {}}
    for segment, robotness, filter_value in (
        ("search", "humans", SEARCH_HUMANS_FILTER),
        ("search", "all", SEARCH_ALL_FILTER),
        ("all", "humans", HUMANS_FILTER),
        ("all", "all", None),
    ):
        geography_details = _detail_rows(
            _stat_with_filter(
                client,
                filter_value,
                **common,
                metrics=SEARCH_DETAIL_METRICS,
                dimensions=f"{REGION_AREA},{REGION_CITY}",
                limit=10000,
                sort="-ym:s:visits",
                lang="ru",
            ),
            2,
        )
        if segment == "search":
            search_engines = _detail_rows(
                _stat_with_filter(
                    client,
                    filter_value,
                    **common,
                    metrics=SEARCH_DETAIL_METRICS,
                    dimensions=LAST_SIGN_SEARCH_ENGINE,
                    limit=100,
                    sort="-ym:s:visits",
                    lang="ru",
                ),
                1,
            )
            landing_pages = _detail_rows(
                _stat_with_filter(
                    client,
                    filter_value,
                    **common,
                    metrics=SEARCH_DETAIL_METRICS,
                    dimensions=f"{LAST_SIGN_SEARCH_ENGINE},{START_URL}",
                    limit=10000,
                    sort="-ym:s:visits",
                    lang="ru",
                ),
                2,
            )
        else:
            search_engines = []
            landing_pages = _detail_rows(
                _stat_with_filter(
                    client,
                    filter_value,
                    **common,
                    metrics=SEARCH_DETAIL_METRICS,
                    dimensions=START_URL,
                    limit=10000,
                    sort="-ym:s:visits",
                    lang="ru",
                ),
                1,
            )
            for landing in landing_pages:
                landing["dimensions"].insert(0, {"id": "all", "name": "Все источники"})
        detail_variants[segment][robotness] = {
            "search_engines": search_engines,
            "search_geography": geography_details,
            "landing_pages": landing_pages,
        }
        for code, value in _geography_totals(geography_details).items():
            points.append(
                {
                    "code": f"segment_{segment}_{robotness}_geography_{code}_visits",
                    "value": str(value),
                    "unit": "count",
                    "dimensions": {
                        "segment": segment,
                        "robotness": robotness,
                        "region_code": code,
                    },
                }
            )
    search_details = detail_variants["search"]
    goals_by_segment = {
        "search": {"humans": [], "all": []},
        "all": {"humans": [], "all": []},
    }
    for goal in mapping.selected_goals:
        goal_id = str(goal["id"])
        dimensions = {
            "goal_id": goal_id,
            "name": goal.get("name", ""),
            "label": goal.get("label", goal.get("name", "")),
            "identifier": goal.get("identifier", ""),
        }
        goal_values = {"search": {}, "all": {}}
        for segment, robotness, filter_value in (
            ("search", "humans", SEARCH_HUMANS_FILTER),
            ("search", "all", SEARCH_ALL_FILTER),
            ("all", "humans", HUMANS_FILTER),
            ("all", "all", None),
        ):
            response = _stat_with_filter(
                client,
                filter_value,
                **common,
                metrics=(
                    f"{GOAL_CONVERSION_RATE.format(id=goal_id)},"
                    f"{GOAL_VISITS.format(id=goal_id)},"
                    f"{GOAL_REACHES.format(id=goal_id)}"
                ),
            )
            goal_row = (response.get("data") or [{"metrics": response.get("totals", [])}])[0]
            goal_values[segment][robotness] = {
                **dimensions,
                "conversion_rate": str(_value(goal_row, 0)),
                "visits": str(_value(goal_row, 1)),
                "reaches": str(_value(goal_row, 2)),
            }
            goals_by_segment[segment][robotness].append(goal_values[segment][robotness])
            for metric_name, unit in (
                ("visits", "count"),
                ("reaches", "count"),
                ("conversion_rate", "percent"),
            ):
                points.append(
                    {
                        "code": (f"segment_{segment}_{robotness}_goal_{goal_id}_{metric_name}"),
                        "value": goal_values[segment][robotness][metric_name],
                        "unit": unit,
                        "dimensions": {
                            **dimensions,
                            "segment": segment,
                            "robotness": robotness,
                        },
                    }
                )
        for metric_name, unit in (
            ("visits", "count"),
            ("reaches", "count"),
            ("conversion_rate", "percent"),
        ):
            points.append(
                {
                    "code": f"goal_{goal_id}_{metric_name}",
                    "value": goal_values["search"]["humans"][metric_name],
                    "unit": unit,
                    "dimensions": dimensions,
                }
            )
    goals_by_robotness = goals_by_segment["search"]
    cleaned_goals = goals_by_robotness["humans"]
    return {
        "period_start": month.isoformat(),
        "period_end": month_end(month).isoformat(),
        "metrics": points,
        "traffic_sources": cleaned_sources,
        "geography": geography_rows,
        "traffic_by_segment": traffic_by_segment,
        "detail_variants": detail_variants,
        "search_details": search_details,
        # Keep direct aliases for older exporters; detailed reports select the
        # requested robotness variant from search_details.
        "search_engines": search_details["humans"]["search_engines"],
        "search_geography": search_details["humans"]["search_geography"],
        "landing_pages": search_details["humans"]["landing_pages"],
        "search_segment": {
            "traffic_source": "organic",
            "robotness": "humans",
            "attribution": "lastsign",
        },
        "goals": cleaned_goals,
        "goals_by_robotness": goals_by_robotness,
        "goals_by_segment": goals_by_segment,
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


def _series(response, name):
    indicators = response.get("indicators", {}) if isinstance(response, dict) else {}
    rows = indicators.get(name, []) if isinstance(indicators, dict) else []
    return rows if isinstance(rows, list) else []


def _valid_dated_values(rows, start=None, end=None):
    values = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        try:
            row_date = date.fromisoformat(str(row["date"])[:10])
        except ValueError:
            continue
        if (start and row_date < start) or (end and row_date > end):
            continue
        value = _number(row.get("value"))
        if value is not None:
            values.append((row_date, value))
    return values


def _last_dated_value(rows, start=None, end=None):
    values = _valid_dated_values(rows, start, end)
    return max(values, key=lambda item: item[0])[1] if values else None


def _response_dates(response, start=None, end=None):
    if not isinstance(response, dict):
        return []
    candidates = []
    for key in ("history", "points", "data"):
        if isinstance(response.get(key), list):
            candidates.extend(response[key])
    indicators = response.get("indicators")
    if isinstance(indicators, dict):
        for rows in indicators.values():
            if isinstance(rows, list):
                candidates.extend(rows)
    dates = []
    for row in candidates:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        try:
            row_date = date.fromisoformat(str(row["date"])[:10])
        except ValueError:
            continue
        if (start and row_date < start) or (end and row_date > end):
            continue
        dates.append(row_date)
    return dates


def _last_metric(rows, names):
    for row in reversed(rows):
        for name in names:
            if name in row:
                value = _number(row[name])
                if value is not None:
                    return value
    return None


def _optional_webmaster_resource(mapping, resource, fetch):
    try:
        return fetch()
    except YandexAPIError as exc:
        if exc.http_status == 404 and exc.error_code in OPTIONAL_WEBMASTER_CODES:
            logger.info(
                "Webmaster resource is unavailable: project=%s resource=%s code=%s",
                mapping.project_id,
                resource,
                exc.error_code,
            )
            return {}
        raise


def _dated_series(response, name, start, end):
    return [
        {"date": day.isoformat(), "value": str(value)}
        for day, value in _valid_dated_values(_series(response, name), start, end)
    ]


def _webmaster_daily_queries(response, start, end):
    indicators = {
        name: dict(_valid_dated_values(_series(response, name), start, end))
        for name in ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION")
    }
    days = sorted({day for values in indicators.values() for day in values})
    rows = []
    for day in days:
        shows = indicators["TOTAL_SHOWS"].get(day)
        clicks = indicators["TOTAL_CLICKS"].get(day)
        ctr = (
            clicks * Decimal(100) / shows if shows not in (None, 0) and clicks is not None else None
        )
        rows.append(
            {
                "date": day.isoformat(),
                "shows": str(shows) if shows is not None else None,
                "clicks": str(clicks) if clicks is not None else None,
                "ctr": str(ctr) if ctr is not None else None,
                "average_position": (
                    str(indicators["AVG_SHOW_POSITION"][day])
                    if day in indicators["AVG_SHOW_POSITION"]
                    else None
                ),
            }
        )
    return rows


def _query_summary(response, start, end):
    daily = _webmaster_daily_queries(response, start, end)
    shows = sum(
        (Decimal(row["shows"]) for row in daily if row.get("shows") is not None), Decimal(0)
    )
    clicks = sum(
        (Decimal(row["clicks"]) for row in daily if row.get("clicks") is not None), Decimal(0)
    )
    weighted = [
        (Decimal(row["shows"]), Decimal(row["average_position"]))
        for row in daily
        if row.get("shows") is not None and row.get("average_position") is not None
    ]
    position_base = sum((item[0] for item in weighted), Decimal(0))
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "shows": str(shows) if daily else None,
        "clicks": str(clicks) if daily else None,
        "ctr": str(clicks * Decimal(100) / shows) if shows else None,
        "average_position": (
            str(sum((count * position for count, position in weighted), Decimal(0)) / position_base)
            if position_base
            else None
        ),
        "daily": daily,
    }


def _popular_queries(response):
    rows = []
    for item in response.get("queries", []):
        indicators = item.get("indicators") or {}
        rows.append(
            {
                "query_id": str(item.get("query_id") or ""),
                "query": str(item.get("query_text") or "").strip(),
                "shows": str(indicators.get("TOTAL_SHOWS"))
                if indicators.get("TOTAL_SHOWS") is not None
                else None,
                "clicks": str(indicators.get("TOTAL_CLICKS"))
                if indicators.get("TOTAL_CLICKS") is not None
                else None,
                "average_position": str(indicators.get("AVG_SHOW_POSITION"))
                if indicators.get("AVG_SHOW_POSITION") is not None
                else None,
                "average_click_position": str(indicators.get("AVG_CLICK_POSITION"))
                if indicators.get("AVG_CLICK_POSITION") is not None
                else None,
            }
        )
    for row in rows:
        shows = _number(row.get("shows"))
        clicks = _number(row.get("clicks"))
        row["ctr"] = (
            str(clicks * Decimal(100) / shows) if shows not in (None, 0) and clicks else "0"
        )
    return rows


def _path_distribution(response):
    paths = Counter()
    for item in response.get("samples", []):
        path = urlsplit(str(item.get("url") or "")).path.strip("/")
        label = "/" + path.split("/", 1)[0] if path else "/"
        paths[label] += 1
    rows = [{"path": key, "count": value} for key, value in paths.most_common(8)]
    known = sum(paths.values())
    available = int(response.get("count") or known)
    if available > known:
        rows.append({"path": "Статус неизвестен", "count": available - known})
    return {
        "rows": rows,
        "sample_count": known,
        "available_count": available,
        "truncated": bool(response.get("truncated")),
    }


def _webmaster_month(
    client, mapping, user_id, month, *, host=None, summary=None, include_current=False
):
    start, end = month, month_end(month)
    params = {"date_from": start.isoformat(), "date_to": end.isoformat()}

    queries = _optional_webmaster_resource(
        mapping,
        "search_queries",
        lambda: client.search_query_history(
            user_id,
            mapping.host_id,
            **params,
            device_type_indicator="ALL",
            query_indicator=["TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION"],
        ),
    )
    pages = _optional_webmaster_resource(
        mapping,
        "pages_in_search",
        lambda: client.search_urls_history(user_id, mapping.host_id, **params),
    )
    indexing = _optional_webmaster_resource(
        mapping,
        "search_events",
        lambda: client.indexing_history(user_id, mapping.host_id, **params),
    )
    sqi = _optional_webmaster_resource(
        mapping,
        "sqi",
        lambda: client.squ_history(user_id, mapping.host_id, **params),
    )
    query_summary = _query_summary(queries, start, end)
    previous_query_summary = None
    popular = []
    previous_popular = []
    path_distribution = None
    if include_current:
        day_count = (end - start).days + 1
        comparison_end = start - timedelta(days=1)
        comparison_start = comparison_end - timedelta(days=day_count - 1)
        comparison_params = {
            "date_from": comparison_start.isoformat(),
            "date_to": comparison_end.isoformat(),
        }
        previous_queries = _optional_webmaster_resource(
            mapping,
            "search_queries_comparison",
            lambda: client.search_query_history(
                user_id,
                mapping.host_id,
                **comparison_params,
                device_type_indicator="ALL",
                query_indicator=["TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION"],
            ),
        )
        previous_query_summary = _query_summary(previous_queries, comparison_start, comparison_end)
        if hasattr(client, "popular_search_queries"):
            query_indicators = [
                "TOTAL_SHOWS",
                "TOTAL_CLICKS",
                "AVG_SHOW_POSITION",
                "AVG_CLICK_POSITION",
            ]
            popular = _popular_queries(
                _optional_webmaster_resource(
                    mapping,
                    "popular_search_queries",
                    lambda: client.popular_search_queries(
                        user_id,
                        mapping.host_id,
                        **params,
                        order_by="TOTAL_CLICKS",
                        query_indicator=query_indicators,
                        device_type_indicator="ALL",
                        offset=0,
                        limit=50,
                    ),
                )
            )
            previous_popular = _popular_queries(
                _optional_webmaster_resource(
                    mapping,
                    "popular_search_queries_comparison",
                    lambda: client.popular_search_queries(
                        user_id,
                        mapping.host_id,
                        **comparison_params,
                        order_by="TOTAL_CLICKS",
                        query_indicator=query_indicators,
                        device_type_indicator="ALL",
                        offset=0,
                        limit=50,
                    ),
                )
            )
        if hasattr(client, "search_urls_samples"):
            path_distribution = _path_distribution(
                _optional_webmaster_resource(
                    mapping,
                    "search_urls_samples",
                    lambda: client.search_urls_samples(user_id, mapping.host_id),
                )
            )
    impressions_values = [
        value for _, value in _valid_dated_values(_series(queries, "TOTAL_SHOWS"), start, end)
    ]
    click_values = [
        value for _, value in _valid_dated_values(_series(queries, "TOTAL_CLICKS"), start, end)
    ]
    impressions = sum(impressions_values, Decimal(0)) if impressions_values else None
    clicks = sum(click_values, Decimal(0)) if click_values else None
    metrics = {}
    if impressions is not None:
        metrics["search_impressions"] = (impressions, "count")
    if clicks is not None:
        metrics["search_clicks"] = (clicks, "count")
    if impressions not in (None, 0) and clicks is not None:
        metrics["search_ctr"] = (clicks * Decimal(100) / impressions, "percent")
    shows_by_date = dict(_valid_dated_values(_series(queries, "TOTAL_SHOWS"), start, end))
    positions_by_date = dict(_valid_dated_values(_series(queries, "AVG_SHOW_POSITION"), start, end))
    weighted_days = [
        (shows, positions_by_date[day])
        for day, shows in shows_by_date.items()
        if day in positions_by_date
    ]
    position_impressions = sum((shows for shows, _ in weighted_days), Decimal(0))
    if position_impressions:
        metrics["average_position"] = (
            sum((shows * position for shows, position in weighted_days), Decimal(0))
            / position_impressions,
            "number",
        )
    aliases = {
        "added_pages": (indexing, ("APPEARED_IN_SEARCH", "added", "appeared")),
        "excluded_pages": (indexing, ("REMOVED_FROM_SEARCH", "excluded", "removed")),
    }
    for code, (response, names) in aliases.items():
        values = []
        for name in names:
            values = [
                value for _, value in _valid_dated_values(_series(response, name), start, end)
            ]
            if values:
                break
        value = sum(values, Decimal(0)) if values else None
        if value is not None:
            metrics[code] = (value, "count" if code != "iks" else "number")
    indexed_pages = _last_dated_value(pages.get("history", []), start, end)
    if indexed_pages is not None:
        metrics["indexed_pages"] = (indexed_pages, "count")
    iks = _last_dated_value(sqi.get("points", []), start, end)
    if iks is not None:
        metrics["iks"] = (iks, "number")
    if include_current and summary:
        for code, names in {
            "searchable_pages_count": ("searchable_pages_count", "searchable_pages"),
            "excluded_pages_count": ("excluded_pages_count", "excluded_pages"),
        }.items():
            value = next((_number(summary.get(name)) for name in names if name in summary), None)
            if value is not None:
                metrics[code] = (value, "count")
    response_dates = [
        item
        for response in (queries, pages, indexing, sqi)
        for item in _response_dates(response, start, end)
    ]
    actual_period = (
        {"date_from": min(response_dates).isoformat(), "date_to": max(response_dates).isoformat()}
        if response_dates
        else None
    )
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "metrics": metrics,
        "site_problems": (summary or {}).get("site_problems", {}),
        "actual_period": actual_period,
        "availability_reason": None if response_dates else "API не вернул данные за период.",
        "daily": {
            "queries": query_summary.get("daily", []),
            "indexed_pages": [
                {"date": day.isoformat(), "value": str(value)}
                for day, value in _valid_dated_values(pages.get("history", []), start, end)
            ],
            "iks": [
                {"date": day.isoformat(), "value": str(value)}
                for day, value in _valid_dated_values(sqi.get("points", []), start, end)
            ],
        },
        "query_summary": {key: value for key, value in query_summary.items() if key != "daily"},
        "comparison_query_summary": previous_query_summary,
        "popular_queries": popular,
        "comparison_popular_queries": previous_popular,
        "path_distribution": path_distribution,
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
            raise YandexAPIError(
                "Выбранный сайт больше не доступен.",
                http_status=404,
                error_code="HOST_NOT_LOADED",
            )
        # The host list already contains the verified host metadata required below.
        # Avoid a redundant host-detail request: it added another failure point and
        # its response was never persisted or used for calculations.
        host = allowed[mapping.host_id]
        summary = _optional_webmaster_resource(
            mapping,
            "summary",
            lambda: client.summary(user_id, mapping.host_id),
        )
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
                    "site_problems": data["site_problems"],
                    "metrics": {key: str(value[0]) for key, value in data["metrics"].items()},
                    "daily": data["daily"],
                    "query_summary": data["query_summary"],
                    "comparison_query_summary": data["comparison_query_summary"],
                    "popular_queries": data["popular_queries"],
                    "comparison_popular_queries": data["comparison_popular_queries"],
                    "path_distribution": data["path_distribution"],
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
    except YandexUnauthorized:
        logger.warning(
            "Webmaster authorization failed: project=%s run=%s",
            mapping.project_id,
            run.id,
        )
        run.status = run.Status.FAILED
        run.completed_at = timezone.now()
        run.error_message = "Нужно повторно авторизовать аккаунт Яндекса для Вебмастера."
        run.save(update_fields=["status", "completed_at", "error_message"])
        return run
    except YandexAPIError as exc:
        logger.warning(
            "Webmaster API failed: project=%s run=%s status=%s code=%s",
            mapping.project_id,
            run.id,
            exc.http_status,
            exc.error_code or "unknown",
        )
        run.status = run.Status.FAILED
        run.completed_at = timezone.now()
        run.error_message = (
            "Проверьте подтверждение выбранного сайта в Яндекс.Вебмастере."
            if exc.error_code == "HOST_NOT_VERIFIED"
            else "Выбранный сайт больше не доступен аккаунту Яндекса. Выберите сайт заново."
            if exc.error_code == "HOST_NOT_LOADED"
            else "Не удалось синхронизировать данные Вебмастера. Повторите позже."
        )
        run.save(update_fields=["status", "completed_at", "error_message"])
        return run
    except Exception:
        logger.exception(
            "Unexpected Webmaster sync failure: project=%s run=%s",
            mapping.project_id,
            run.id,
        )
        run.status = run.Status.FAILED
        run.completed_at = timezone.now()
        run.error_message = "Не удалось синхронизировать данные Вебмастера. Повторите позже."
        run.save(update_fields=["status", "completed_at", "error_message"])
        return run
