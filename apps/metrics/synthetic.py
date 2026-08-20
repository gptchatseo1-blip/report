import calendar
import hashlib
import json
from datetime import date, timedelta
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
    metrics.extend(
        (
            ("geography_moscow_visits", round(visits * 0.42), MetricPoint.Unit.COUNT),
            (
                "geography_saint_petersburg_visits",
                round(visits * 0.09),
                MetricPoint.Unit.COUNT,
            ),
            ("geography_undefined_visits", round(visits * 0.025), MetricPoint.Unit.COUNT),
            (
                "geography_area_undefined_visits",
                round(visits * 0.015),
                MetricPoint.Unit.COUNT,
            ),
        )
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


def _split_total(total, days, seed=0):
    weights = [90 + ((index * 17 + seed) % 23) for index in range(days)]
    weight_sum = sum(weights)
    values = [round(total * weight / weight_sum) for weight in weights]
    values[-1] += total - sum(values)
    return values


def _metrika_details(project, month, metrics):
    values = {code: Decimal(str(value)) for code, value, _unit in metrics}
    search_visits = int(values["source_search_visits"])
    domain = project.normalized_domain
    engine_weights = (
        ("google", "Google", 0.67, "18.75"),
        ("yandex", "Яндекс", 0.31, "16.19"),
        ("bing", "Bing", 0.012, "12.00"),
        ("duckduckgo", "DuckDuckGo", 0.008, "10.00"),
    )
    engines = []
    allocated = 0
    for index, (engine_id, name, weight, bounce) in enumerate(engine_weights):
        visits = (
            search_visits - allocated
            if index == len(engine_weights) - 1
            else round(search_visits * weight)
        )
        allocated += visits
        engines.append(
            {
                "dimensions": [{"id": engine_id, "name": name}],
                "visits": str(visits),
                "users": str(round(visits * 0.84)),
                "bounce_rate": bounce,
            }
        )
    geography_specs = (
        ("Центральный федеральный округ", "Москва", 0.203, "15.10"),
        ("Северо-Западный федеральный округ", "Санкт-Петербург", 0.071, "16.80"),
        ("Не определено", "Не определено", 0.025, "21.00"),
        ("Область не определена", "Не определено", 0.018, "19.40"),
    )
    geography = [
        {
            "dimensions": [{"id": area, "name": area}, {"id": city, "name": city}],
            "visits": str(round(search_visits * weight)),
            "users": str(round(search_visits * weight * 0.83)),
            "bounce_rate": bounce,
        }
        for area, city, weight, bounce in geography_specs
    ]
    landing_specs = (
        ("yandex", "Яндекс", f"https://{domain}/", 0.075),
        ("yandex", "Яндекс", f"https://{domain}/blog/", 0.145),
        ("yandex", "Яндекс", f"https://{domain}/services/", 0.091),
        ("yandex", "Яндекс", f"https://{domain}/services/priority/1", 0.052),
        ("google", "Google", f"https://{domain}/", 0.078),
        ("google", "Google", f"https://{domain}/blog/article-1/", 0.237),
        ("google", "Google", f"https://{domain}/catalog/diagnostics/", 0.181),
        ("google", "Google", f"https://{domain}/services/priority/2", 0.141),
    )
    landings = []
    for engine_id, engine_name, url, weight in landing_specs:
        visits = round(search_visits * weight)
        landings.append(
            {
                "dimensions": [
                    {"id": engine_id, "name": engine_name},
                    {"id": url, "name": url},
                ],
                "visits": str(visits),
                "users": str(round(visits * 0.86)),
                "bounce_rate": str(_decimal(12 + weight * 35)),
            }
        )

    def scale(rows, multiplier):
        result = []
        for row in rows:
            item = {**row, "dimensions": [dict(value) for value in row["dimensions"]]}
            item["visits"] = str(round(Decimal(row["visits"]) * multiplier))
            item["users"] = str(round(Decimal(row["users"]) * multiplier))
            result.append(item)
        return result

    goals = [
        {
            "goal_id": "request",
            "name": "Записаться на приём",
            "label": "Записаться на приём",
            "identifier": "request_form",
            "conversion_rate": "3.01",
            "visits": str(round(search_visits * 0.031)),
            "reaches": str(round(search_visits * 0.035)),
        },
        {
            "goal_id": "callback",
            "name": "Заказ обратного звонка",
            "label": "Заказ обратного звонка",
            "identifier": "callback",
            "conversion_rate": "0.25",
            "visits": str(round(search_visits * 0.0025)),
            "reaches": str(round(search_visits * 0.0028)),
        },
    ]
    return {
        "search_details": {
            "humans": {
                "search_engines": engines,
                "search_geography": geography,
                "landing_pages": landings,
            },
            "all": {
                "search_engines": scale(engines, Decimal("1.04")),
                "search_geography": scale(geography, Decimal("1.04")),
                "landing_pages": scale(landings, Decimal("1.04")),
            },
        },
        "search_engines": engines,
        "search_geography": geography,
        "landing_pages": landings,
        "search_segment": {
            "traffic_source": "organic",
            "robotness": "humans",
            "attribution": "lastsign",
        },
        "goals": goals,
        "goals_by_robotness": {"humans": goals, "all": goals},
    }


def _webmaster_details(project, month, metrics):
    values = {code: Decimal(str(value)) for code, value, _unit in metrics}
    days = month_end(month).day
    shows = _split_total(int(values["search_impressions"]), days, month.month)
    clicks = _split_total(int(values["search_clicks"]), days, month.month + 7)
    position = values["average_position"]
    indexed = int(values["indexed_pages"])
    iks = int(values["quality_index"])
    query_rows = []
    indexing_rows = []
    iks_rows = []
    for index in range(days):
        day = month + timedelta(days=index)
        day_position = position + Decimal((index % 7) - 3) / Decimal(20)
        query_rows.append(
            {
                "date": day.isoformat(),
                "shows": str(shows[index]),
                "clicks": str(clicks[index]),
                "ctr": str(_decimal(clicks[index] * 100 / shows[index])),
                "average_position": str(_decimal(day_position)),
            }
        )
        indexing_rows.append(
            {
                "date": day.isoformat(),
                "value": str(indexed + round((index - days / 2) * 2.4)),
            }
        )
        iks_rows.append(
            {
                "date": day.isoformat(),
                "value": str(iks - 10 if index < days // 3 else iks),
            }
        )
    query_summary = {
        "period_start": month.isoformat(),
        "period_end": month_end(month).isoformat(),
        "shows": str(sum(shows)),
        "clicks": str(sum(clicks)),
        "ctr": str(_decimal(sum(clicks) * 100 / sum(shows))),
        "average_position": str(position),
        "daily": query_rows,
    }
    previous_summary = {
        "period_start": (month - timedelta(days=days)).isoformat(),
        "period_end": (month - timedelta(days=1)).isoformat(),
        "shows": str(round(sum(shows) * 0.84)),
        "clicks": str(round(sum(clicks) * 0.98)),
        "ctr": str(_decimal(Decimal(query_summary["ctr"]) + Decimal("0.45"))),
        "average_position": str(_decimal(position - Decimal("0.19"))),
    }
    popular = []
    previous_popular = []
    for index in range(1, 13):
        row_shows = max(40, round(sum(shows) / (index + 8)))
        row_clicks = max(5, round(sum(clicks) / (index + 10)))
        query = f"пример кликабельного запроса {index}"
        popular.append(
            {
                "query_id": str(index),
                "query": query,
                "shows": str(row_shows),
                "clicks": str(row_clicks),
                "ctr": str(_decimal(row_clicks * 100 / row_shows)),
                "average_position": str(_decimal(Decimal("2.1") + index / Decimal(3))),
                "average_click_position": str(_decimal(Decimal("1.8") + index / Decimal(4))),
            }
        )
        previous_popular.append(
            {
                "query_id": str(index),
                "query": query,
                "shows": str(round(row_shows * (0.88 if index % 2 else 1.09))),
                "clicks": str(round(row_clicks * (0.92 if index % 3 else 1.12))),
                "ctr": str(_decimal(row_clicks * 100 / row_shows + (0.5 if index % 2 else -0.3))),
                "average_position": str(_decimal(Decimal("2.3") + index / Decimal(3))),
            }
        )
    return {
        "daily": {"queries": query_rows, "indexed_pages": indexing_rows, "iks": iks_rows},
        "query_summary": query_summary,
        "comparison_query_summary": previous_summary,
        "popular_queries": popular,
        "comparison_popular_queries": previous_popular,
        "path_distribution": {
            "rows": [
                {"path": "/services", "count": round(indexed * 0.46)},
                {"path": "/blog", "count": round(indexed * 0.28)},
                {"path": "/catalog", "count": round(indexed * 0.17)},
                {"path": "Статус неизвестен", "count": round(indexed * 0.09)},
            ],
            "sample_count": indexed,
            "available_count": indexed,
            "truncated": False,
        },
    }


BUILDERS = {
    SourceSnapshot.Source.METRIKA: _metrika_payload,
    SourceSnapshot.Source.WEBMASTER: _webmaster_payload,
}


def build_synthetic_payload(project, source, month):
    metrics = BUILDERS[source](project, month)
    payload = {
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
    if source == SourceSnapshot.Source.METRIKA:
        payload.update(_metrika_details(project, month, metrics))
    elif source == SourceSnapshot.Source.WEBMASTER:
        payload.update(_webmaster_details(project, month, metrics))
    return payload


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
