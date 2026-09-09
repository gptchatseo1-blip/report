import base64
import hashlib
import io
import json
import logging
import re
import urllib.request
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Max
from django.db.models.fields.json import KeyTransform
from django.utils import timezone
from PIL import Image

from apps.metrics.models import MetricPoint, RankingSnapshot, SourceSnapshot
from apps.worklog.models import WorkLogItem

from .calculations import (
    FORMULA_VERSION,
    ChangeKind,
    PositionItem,
    calculate_change,
    calculate_periods,
    calculate_position_distribution,
    calculate_source_shares,
    check_ctr,
    compare_semantics,
    depth_comment,
    shift_month,
    top_11_20_rows,
)
from .models import (
    GeneratedArtifact,
    Report,
    ReportDatasetSnapshot,
    ReportVersion,
    ValidationIssue,
)

SNAPSHOT_SCHEMA_VERSION = "mvp1.1"
logger = logging.getLogger(__name__)
SENSITIVE_SOURCE_KEY_RE = re.compile(
    r"(?i)^(?:api[_ -]?key|authorization|oauth[_ -]?token|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|password|cookie|set[_ -]?cookie)$"
)
SENSITIVE_SOURCE_TEXT_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*(?:bearer|basic|oauth)\s+\S+|"
    r"sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,})"
)


def redact_sensitive_source_data(value):
    """Remove credentials accidentally retained in legacy provider payloads."""
    if isinstance(value, dict):
        return {
            str(key): (
                "redacted"
                if SENSITIVE_SOURCE_KEY_RE.fullmatch(str(key).strip())
                else redact_sensitive_source_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_sensitive_source_data(item) for item in value]
    if isinstance(value, str) and SENSITIVE_SOURCE_TEXT_RE.search(value):
        return "redacted"
    return value


def _report_metrika_payload(payload, display_options):
    """Freeze only Metrika variants that the immutable report can render."""
    if not payload.get("detail_variants"):
        # Snapshots created before segmented imports keep their report rows in
        # the direct aliases. They are already substantially smaller.
        return redact_sensitive_source_data(payload)
    options = display_options or {}
    robotness = options.get("metrika_robotness")
    if robotness not in {"humans", "all"}:
        robotness = "humans"
    segment = "search" if options.get("metrika_search_segment", True) else "all"
    goal_robotness = "humans" if options.get("metrika_goals_humans_only", True) else "all"
    bulky_aliases = {
        "detail_variants",
        "search_details",
        "search_engines",
        "search_geography",
        "landing_pages",
        "traffic_source_variants",
        "traffic_source_details",
        "traffic_source_total",
        "traffic_source_quarter_variants",
        "traffic_source_quarter_details",
        "traffic_source_quarter_total",
        "goals",
        "goals_by_robotness",
        "goals_by_segment",
    }
    compact = {key: value for key, value in payload.items() if key not in bulky_aliases}

    detail_variants = payload.get("detail_variants") or {}
    needed_segments = {segment, "search"}
    compact["detail_variants"] = {
        name: {robotness: (detail_variants.get(name) or {}).get(robotness) or {}}
        for name in needed_segments
    }
    traffic_variant = (payload.get("traffic_source_variants") or {}).get(robotness) or {}
    compact["traffic_source_variants"] = {robotness: traffic_variant}
    quarter_variant = (payload.get("traffic_source_quarter_variants") or {}).get(robotness) or {}
    if quarter_variant:
        compact["traffic_source_quarter_variants"] = {robotness: quarter_variant}
    goal_rows = ((payload.get("goals_by_segment") or {}).get(segment) or {}).get(
        goal_robotness
    ) or []
    compact["goals_by_segment"] = {segment: {goal_robotness: goal_rows}}
    return redact_sensitive_source_data(compact)


def _json_path(*keys):
    """Build a JSON key transform without selecting the complete JSON column."""
    expression = "payload"
    for key in keys:
        expression = KeyTransform(key, expression)
    return expression


def _metrika_payload_annotations(display_options):
    """Return only report-visible Metrika JSON branches from PostgreSQL.

    A synchronized source snapshot can contain four copies of 10,000-row detail
    reports.  Loading ``SourceSnapshot.payload`` and compacting it in Python makes
    the web worker decode every copy first, which can consume more than 1 GB.  JSON
    key transforms make the database return only the selected branches.
    """
    options = display_options or {}
    robotness = options.get("metrika_robotness")
    if robotness not in {"humans", "all"}:
        robotness = "humans"
    segment = "search" if options.get("metrika_search_segment", True) else "all"
    goal_robotness = "humans" if options.get("metrika_goals_humans_only", True) else "all"
    return {
        "report_payload_schema_version": _json_path("schema_version"),
        "report_payload_source": _json_path("source"),
        "report_payload_retrieval_method": _json_path("retrieval_method"),
        "report_payload_counter_id": _json_path("counter_id"),
        "report_payload_sync_fingerprint": _json_path("sync_fingerprint"),
        "report_payload_retrieved_at": _json_path("retrieved_at"),
        "report_payload_contains_sensitive_data": _json_path("contains_sensitive_data"),
        "report_payload_unavailable_goal_ids": _json_path("unavailable_goal_ids"),
        "report_payload_sampled": _json_path("sampled"),
        "report_payload_sample_share": _json_path("sample_share"),
        "report_payload_search_segment": _json_path("search_segment"),
        "report_detail_selected_landing_total": _json_path(
            "detail_variants", segment, robotness, "landing_pages_total"
        ),
        "report_detail_selected_geography_total": _json_path(
            "detail_variants", segment, robotness, "search_geography_total"
        ),
        "report_detail_search_engines": _json_path(
            "detail_variants", "search", robotness, "search_engines"
        ),
        "report_detail_search_engines_total": _json_path(
            "detail_variants", "search", robotness, "search_engines_total"
        ),
        "report_detail_search_landing_total": _json_path(
            "detail_variants", "search", robotness, "landing_pages_total"
        ),
        "report_traffic_source_variant": _json_path("traffic_source_variants", robotness),
        "report_traffic_source_quarter_variant": _json_path(
            "traffic_source_quarter_variants", robotness
        ),
        "report_goal_rows": _json_path("goals_by_segment", segment, goal_robotness),
    }


def _snapshot_json_branch(snapshot, *keys):
    """Read one JSON branch without decoding the complete source snapshot."""
    return (
        SourceSnapshot.objects.filter(pk=snapshot.pk)
        .values_list(_json_path(*keys), flat=True)
        .get()
    )


def _report_metrika_payload_from_snapshot(snapshot, display_options, *, include_large_details=True):
    """Build the compact report payload from DB-extracted JSON branches."""
    selected_landing_total = getattr(snapshot, "report_detail_selected_landing_total", None)
    search_landing_total = getattr(snapshot, "report_detail_search_landing_total", None)
    if (
        getattr(snapshot, "report_payload_schema_version", None) is None
        and selected_landing_total is None
        and search_landing_total is None
    ):
        # Compatibility path for old, substantially smaller source snapshots.
        return _report_metrika_payload(snapshot.payload, display_options)

    options = display_options or {}
    robotness = options.get("metrika_robotness")
    if robotness not in {"humans", "all"}:
        robotness = "humans"
    segment = "search" if options.get("metrika_search_segment", True) else "all"
    goal_robotness = "humans" if options.get("metrika_goals_humans_only", True) else "all"
    configured = str(options.get("configuration_version")) in {"2", "3"}
    include_geography = configured and options.get("include_metrika_geography", False)
    include_landing = not configured or any(
        options.get(name, False)
        for name in (
            "include_metrika_landing_pages",
            "include_metrika_landing_page_comparison",
            "include_metrika_url_groups",
            "include_metrika_sections",
            "include_metrika_categories",
        )
    )
    include_landing_history = not configured or any(
        options.get(name, False)
        for name in (
            "include_metrika_url_groups",
            "include_metrika_sections",
            "include_metrika_categories",
        )
    )
    include_search_landing = not configured or options.get(
        "include_metrika_landing_page_comparison", False
    )
    compact = {}
    for key in (
        "schema_version",
        "source",
        "retrieval_method",
        "counter_id",
        "sync_fingerprint",
        "retrieved_at",
        "contains_sensitive_data",
        "unavailable_goal_ids",
        "sampled",
        "sample_share",
        "search_segment",
    ):
        value = getattr(snapshot, f"report_payload_{key}", None)
        if value is not None:
            compact[key] = value
    selected_detail = {
        "landing_pages_total": selected_landing_total or {},
        "search_geography_total": (
            getattr(snapshot, "report_detail_selected_geography_total", None) or {}
        ),
    }
    if include_large_details and include_geography:
        selected_detail["search_geography"] = (
            _snapshot_json_branch(
                snapshot, "detail_variants", segment, robotness, "search_geography"
            )
            or []
        )
    if (include_large_details or include_landing_history) and include_landing:
        selected_detail["landing_pages"] = (
            _snapshot_json_branch(snapshot, "detail_variants", segment, robotness, "landing_pages")
            or []
        )

    search_detail = {
        "search_engines": getattr(snapshot, "report_detail_search_engines", None) or [],
        "search_engines_total": (
            getattr(snapshot, "report_detail_search_engines_total", None) or {}
        ),
        "landing_pages_total": search_landing_total or {},
    }
    if segment == "search":
        search_detail.update(selected_detail)
    elif include_large_details and include_search_landing:
        search_detail["landing_pages"] = (
            _snapshot_json_branch(snapshot, "detail_variants", "search", robotness, "landing_pages")
            or []
        )
    # The provider hierarchy repeats up to 30,000 URL rows per month. The
    # exporter derives the same two-level hierarchy from landing pages.
    compact["detail_variants"] = {"search": {robotness: search_detail}}
    if segment != "search":
        compact["detail_variants"][segment] = {robotness: selected_detail}
    compact["traffic_source_variants"] = {
        robotness: getattr(snapshot, "report_traffic_source_variant", None) or {}
    }
    quarter = getattr(snapshot, "report_traffic_source_quarter_variant", None) or {}
    if quarter:
        compact["traffic_source_quarter_variants"] = {robotness: quarter}
    compact["goals_by_segment"] = {
        segment: {goal_robotness: getattr(snapshot, "report_goal_rows", None) or []}
    }
    # Every copied key is an allow-listed metric branch produced by our own
    # synchronizer, so no second full object-tree copy is needed here.
    return compact


def _project_favicon(domain):
    """Freeze a small public favicon in the snapshot; test domains never trigger I/O."""
    if not getattr(settings, "REPORT_FAVICON_FETCH_ENABLED", True):
        return None
    domain = str(domain or "").strip().casefold()
    if not domain or domain.endswith((".example", ".test", ".invalid", ".localhost")):
        return None
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=32"
    request = urllib.request.Request(url, headers={"User-Agent": "SEO-Report/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            raw = response.read(128 * 1024 + 1)
        if not raw or len(raw) > 128 * 1024:
            return None
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            icon = source.convert("RGBA")
            output = io.BytesIO()
            icon.save(output, format="PNG")
        return {"mime_type": "image/png", "data": base64.b64encode(output.getvalue()).decode()}
    except (OSError, ValueError):
        logger.info("Project favicon was not available for %s", domain)
        return None


class ReportVersionDeleteBlocked(Exception):
    """Raised when deleting a version would race with an active export."""


def _delete_artifact_file(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception("Failed to remove artifact file after report version deletion")


def build_position_facts(
    *, project, report_month, selected_dates=None, selected_configurations=None
):
    """Build facts independently for every search-engine/region pair; device is absent by design."""
    periods = calculate_periods(report_month)
    snapshot_filter = {"project": project}
    selected_by_engine = selected_dates if isinstance(selected_dates, dict) else None
    flat_dates = (
        {day for item in selected_configurations.values() for day in item.get("dates", ())}
        if selected_configurations
        else {day for dates in (selected_by_engine or {}).values() for day in dates}
    )
    if selected_dates:
        snapshot_filter["snapshot_date__in"] = flat_dates if selected_by_engine else selected_dates
    else:
        snapshot_filter["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**snapshot_filter)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    grouped = defaultdict(dict)
    grouped_daily = defaultdict(dict)
    # Explicit calendar dates are authoritative for chart points too. Reusing this
    # queryset also avoids a second database query for the whole quarter.
    for snapshot in snapshots:
        configuration_selection = (selected_configurations or {}).get(
            str(snapshot.topvisor_configuration_id)
        )
        if selected_configurations and (
            not configuration_selection
            or snapshot.snapshot_date not in configuration_selection.get("dates", ())
        ):
            continue
        if selected_by_engine and snapshot.snapshot_date not in selected_by_engine.get(
            snapshot.search_engine, ()
        ):
            continue
        segment_key = (snapshot.search_engine, snapshot.region, snapshot.topvisor_configuration_id)
        daily_existing = grouped_daily[segment_key].get(snapshot.snapshot_date)
        daily_existing_key = (
            (daily_existing.created_at, str(daily_existing.id)) if daily_existing else None
        )
        if (
            daily_existing_key is None
            or (snapshot.created_at, str(snapshot.id)) > daily_existing_key
        ):
            grouped_daily[segment_key][snapshot.snapshot_date] = snapshot
    for snapshot in snapshots:
        configuration_selection = (selected_configurations or {}).get(
            str(snapshot.topvisor_configuration_id)
        )
        if selected_configurations and (
            not configuration_selection
            or snapshot.snapshot_date not in configuration_selection.get("dates", ())
        ):
            continue
        if selected_by_engine and snapshot.snapshot_date not in selected_by_engine.get(
            snapshot.search_engine, ()
        ):
            continue
        month = snapshot.snapshot_date if selected_dates else snapshot.snapshot_date.replace(day=1)
        # The latest snapshot inside a calendar month wins deterministically.
        segment_key = (snapshot.search_engine, snapshot.region, snapshot.topvisor_configuration_id)
        existing = grouped[segment_key].get(month)
        snapshot_key = (snapshot.snapshot_date, snapshot.created_at, str(snapshot.id))
        existing_key = (
            (existing.snapshot_date, existing.created_at, str(existing.id)) if existing else None
        )
        if existing_key is None or snapshot_key > existing_key:
            grouped[segment_key][month] = snapshot
    facts = []
    engine_order = {"yandex": 0, "google": 1}
    for (engine, region, configuration_id), values in sorted(
        grouped.items(), key=lambda item: (engine_order.get(item[0][0], 99), item[0][1], item[0][2])
    ):
        configuration_selection = (selected_configurations or {}).get(str(configuration_id))
        months = (
            tuple(configuration_selection.get("dates", ()))
            if configuration_selection
            else tuple(selected_by_engine.get(engine, ()))
            if selected_by_engine
            else (
                tuple(selected_dates)
                if selected_dates
                else tuple(shift_month(periods.report.start, offset) for offset in (-2, -1, 0))
            )
        )
        report_snapshot = values.get(months[-1])
        previous_snapshot = values.get(months[0] if selected_dates else periods.previous.start)
        report_rows = tuple(report_snapshot.positions.all()) if report_snapshot else ()
        previous_rows = tuple(previous_snapshot.positions.all()) if previous_snapshot else ()
        comparison_depth = (
            min(report_snapshot.ranking_depth, previous_snapshot.ranking_depth)
            if report_snapshot and previous_snapshot
            else None
        )
        distribution = calculate_position_distribution(
            (
                PositionItem(
                    row.normalized_query,
                    row.frequency,
                    row.position_value,
                    row.group_name,
                    row.normalized_target_url,
                )
                for row in report_rows
            ),
            ranking_depth=report_snapshot.ranking_depth if report_snapshot else 100,
        )
        monthly_series = []
        for month in months:
            snapshot = values.get(month)
            if snapshot is None:
                continue
            monthly_series.append(
                {
                    "month": month,
                    "visibility": snapshot.visibility,
                    "distribution": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in snapshot.positions.all()
                        ),
                        ranking_depth=snapshot.ranking_depth,
                    ),
                    "ranking_depth": snapshot.ranking_depth,
                }
            )
        chart_series = []
        for snapshot_day, snapshot in sorted(
            grouped_daily[(engine, region, configuration_id)].items()
        ):
            chart_series.append(
                {
                    "month": snapshot_day,
                    "visibility": snapshot.visibility,
                    "distribution": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in snapshot.positions.all()
                        ),
                        ranking_depth=snapshot.ranking_depth,
                    ),
                    "ranking_depth": snapshot.ranking_depth,
                }
            )
        facts.append(
            {
                "search_engine": engine,
                "region": region,
                "configuration_id": configuration_id,
                "distribution": distribution,
                "visibility_change": calculate_change(
                    report_snapshot.visibility if report_snapshot else None,
                    previous_snapshot.visibility if previous_snapshot else None,
                    kind=ChangeKind.PERCENTAGE_POINTS,
                ),
                "ranking_depth": report_snapshot.ranking_depth if report_snapshot else None,
                "depth_comment": depth_comment(report_snapshot.ranking_depth)
                if engine == "google" and report_snapshot
                else None,
                "comparison_depth": comparison_depth,
                "comparison_distributions": {
                    "previous": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in previous_rows
                        ),
                        ranking_depth=comparison_depth,
                    ),
                    "current": calculate_position_distribution(
                        (
                            PositionItem(row.normalized_query, row.frequency, row.position_value)
                            for row in report_rows
                        ),
                        ranking_depth=comparison_depth,
                    ),
                }
                if comparison_depth
                else None,
                "warnings": (
                    {
                        "code": "ranking_depth_changed",
                        "previous_depth": previous_snapshot.ranking_depth,
                        "current_depth": report_snapshot.ranking_depth,
                        "visibility_comparable": False,
                    },
                )
                if report_snapshot
                and previous_snapshot
                and report_snapshot.ranking_depth != previous_snapshot.ranking_depth
                else (),
                "three_month_series": tuple(monthly_series),
                "chart_series": tuple(chart_series),
                "semantics": compare_semantics(
                    (row.normalized_query for row in previous_rows),
                    (row.normalized_query for row in report_rows),
                ),
                "top_11_20": top_11_20_rows(
                    (
                        PositionItem(
                            row.normalized_query,
                            row.frequency,
                            row.position_value,
                            row.group_name,
                            row.normalized_target_url,
                        )
                        for row in report_rows
                    ),
                    depth=report_snapshot.ranking_depth if report_snapshot else 0,
                    mode=project.top_11_20_mode,
                ),
            }
        )
    return {"formula_version": FORMULA_VERSION, "periods": periods, "segments": facts}


def build_source_facts(
    *,
    project,
    report_month,
    selected_snapshot_ids=None,
    display_options=None,
    _single_webmaster=False,
):
    """Calculate each source exclusively from its independently selected snapshots."""
    periods = calculate_periods(report_month)
    selected_snapshot_ids = selected_snapshot_ids or {}
    result = {}
    for source in (SourceSnapshot.Source.METRIKA, SourceSnapshot.Source.WEBMASTER):
        ids = selected_snapshot_ids.get(source)
        if ids is None:  # Backward compatibility for old/programmatic snapshots only.
            rows = SourceSnapshot.objects.filter(
                project=project,
                source=source,
                period_start__range=(periods.three_months.start, periods.report.start),
            )
        else:
            rows = SourceSnapshot.objects.filter(project=project, source=source, id__in=ids)
        options = display_options or {}
        if source == SourceSnapshot.Source.METRIKA:
            rows = rows.defer("payload").annotate(**_metrika_payload_annotations(options))
        snapshots = list(
            rows.prefetch_related("metrics").order_by("period_start", "period_end", "id")
        )
        points = []
        all_traffic_totals = []
        segment = "search" if options.get("metrika_search_segment", True) else "all"
        robotness = options.get("metrika_robotness")
        if robotness not in {"humans", "all"}:
            robotness = "humans"
        prefix = f"segment_{segment}_{robotness}_"
        for snapshot in snapshots:
            raw_metrics = {point.metric_code: point for point in snapshot.metrics.all()}
            all_traffic_totals.append(raw_metrics.get("visits"))
            metrics = {
                code: point
                for code, point in raw_metrics.items()
                if not code.startswith("segment_")
                and not code.startswith("source_humans_")
                and not code.startswith("source_all_")
            }
            if source == SourceSnapshot.Source.METRIKA:
                metrics.update(
                    {
                        code.removeprefix(prefix): point
                        for code, point in raw_metrics.items()
                        if code.startswith(prefix)
                    }
                )
                source_prefix = f"source_{robotness}_"
                selected_source_metrics = {
                    f"source_{code.removeprefix(source_prefix)}": point
                    for code, point in raw_metrics.items()
                    if code.startswith(source_prefix)
                }
                if selected_source_metrics:
                    metrics.update(selected_source_metrics)
                elif robotness == "all":
                    metrics = {
                        code: point
                        for code, point in metrics.items()
                        if not code.startswith("source_")
                    }
            points.append((snapshot, metrics))
        # One point has no comparison period: do not manufacture a zero change.
        first = points[0][1] if len(points) >= 2 else {}
        current = points[-1][1] if points else {}

        def monthly_total(point, snapshot):
            if point is None:
                return None
            # Metrika and Webmaster snapshots already contain totals for the
            # selected calendar month. Report comparisons must use those totals
            # directly; dividing by the number of days obscures the figures that
            # users see in the provider interfaces.
            return point.numeric_value

        codes = sorted({code for _snapshot, metrics in points for code in metrics})
        series = {
            code: [
                {
                    "month": snapshot.period_start,
                    "value": monthly_total(metrics.get(code), snapshot),
                }
                for snapshot, metrics in points
            ]
            for code in codes
        }
        changes = {}
        for code in sorted(set(first) | set(current)):
            old, new = first.get(code), current.get(code)
            unit = (new or old).unit
            kind = (
                ChangeKind.PERCENTAGE_POINTS
                if unit == MetricPoint.Unit.PERCENT
                else ChangeKind.VALUE
            )
            changes[code] = calculate_change(
                monthly_total(new, points[-1][0]) if points else None,
                monthly_total(old, points[0][0]) if points else None,
                kind=kind,
            )
        extra = {}
        period_details = []
        large_detail_start = max(0, len(points) - 2)
        for index, (snapshot, _metrics) in enumerate(points):
            period_details.append(
                {
                    "period_start": snapshot.period_start,
                    "period_end": snapshot.period_end,
                    "source_key": snapshot.source_key,
                    "host_url": (
                        ""
                        if source == SourceSnapshot.Source.METRIKA
                        else snapshot.payload.get("host_url", "")
                    ),
                    "payload": (
                        _report_metrika_payload_from_snapshot(
                            snapshot,
                            options,
                            include_large_details=index >= large_detail_start,
                        )
                        if source == SourceSnapshot.Source.METRIKA
                        else redact_sensitive_source_data(snapshot.payload)
                    ),
                }
            )
        extra["period_details"] = period_details
        if source == SourceSnapshot.Source.METRIKA:
            source_api_total = None
            if snapshots:
                source_variant = getattr(snapshots[-1], "report_traffic_source_variant", None) or {}
                total_payload = source_variant.get("total") or (
                    # Legacy snapshots do not have extracted variants and are
                    # small enough for the compatibility path above.
                    snapshots[-1].payload.get("traffic_source_total") or {}
                    if source_variant == {}
                    and getattr(snapshots[-1], "report_payload_schema_version", None) is None
                    and robotness == "humans"
                    else {}
                )
                raw_total = total_payload.get("visits")
                try:
                    source_api_total = Decimal(str(raw_total)) if raw_total is not None else None
                except (ArithmeticError, ValueError):
                    source_api_total = None
            all_traffic_total = (
                source_api_total
                if source_api_total is not None
                else (
                    all_traffic_totals[-1].numeric_value
                    if all_traffic_totals and all_traffic_totals[-1]
                    else None
                )
            )
            sources = {
                code.removeprefix("source_").removesuffix("_visits"): point.numeric_value
                for code, point in current.items()
                if code.startswith("source_")
            }
            extra["traffic_sources"] = calculate_source_shares(all_traffic_total, sources)
            extra["traffic_source_series"] = {
                code.removeprefix("source_").removesuffix("_visits"): series[code]
                for code in codes
                if code.startswith("source_")
            }
            extra["traffic_source_dynamics"] = {}
            for name, values in extra["traffic_source_series"].items():
                raw = sources.get(name)
                extra["traffic_source_dynamics"][name] = {
                    "series": values,
                    "share_percent": raw * Decimal("100") / all_traffic_total
                    if raw is not None and all_traffic_total not in (None, 0)
                    else None,
                    "change": calculate_change(
                        values[-1]["value"] if values else None,
                        values[0]["value"] if values else None,
                        kind=ChangeKind.VALUE,
                    ),
                }
        else:
            extra["ctr_check"] = check_ctr(
                current["search_clicks"].numeric_value if "search_clicks" in current else None,
                current["search_impressions"].numeric_value
                if "search_impressions" in current
                else None,
                current["search_ctr"].numeric_value if "search_ctr" in current else None,
            )
        result[source] = {"normalized_changes": changes, "three_month_series": series, **extra}
    sites = []
    if not _single_webmaster:
        ids = selected_snapshot_ids.get(SourceSnapshot.Source.WEBMASTER)
        webmaster_rows = SourceSnapshot.objects.filter(
            project=project, source=SourceSnapshot.Source.WEBMASTER
        )
        if ids is None:
            webmaster_rows = webmaster_rows.filter(
                period_start__range=(periods.three_months.start, periods.report.start)
            )
        else:
            webmaster_rows = webmaster_rows.filter(id__in=ids)
        mappings = list(project.yandex_webmaster_mappings.order_by("id"))
        mapping_by_key = {item.host_id: item for item in mappings}
        grouped = {}
        for row in webmaster_rows.order_by("period_start", "id"):
            key = row.source_key or row.payload.get("host_id") or row.payload.get("host_url") or ""
            grouped.setdefault(key, []).append(row)
        ordered_keys = [item.host_id for item in mappings if item.host_id in grouped]
        ordered_keys.extend(key for key in grouped if key not in ordered_keys)
        for order, key in enumerate(ordered_keys):
            rows = grouped[key]
            mapping = mapping_by_key.get(key)
            site_result = build_source_facts(
                project=project,
                report_month=report_month,
                selected_snapshot_ids={
                    SourceSnapshot.Source.METRIKA: [],
                    SourceSnapshot.Source.WEBMASTER: [str(row.id) for row in rows],
                },
                display_options=display_options,
                _single_webmaster=True,
            )["sources"][SourceSnapshot.Source.WEBMASTER]
            sites.append(
                {
                    "source_key": key,
                    "host_url": rows[-1].payload.get("host_url") or key,
                    "include_iks": mapping.include_iks if mapping else True,
                    "order": order,
                    "facts": site_result,
                }
            )
        if sites:
            result[SourceSnapshot.Source.WEBMASTER] = sites[0]["facts"]
    return {
        "formula_version": FORMULA_VERSION,
        "periods": periods,
        "sources": result,
        "webmaster_sites": sites,
    }


def _json_value_in_place(value):
    """Normalize a freshly built snapshot without duplicating its whole object graph."""
    if is_dataclass(value):
        return _json_value_in_place(asdict(value))
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            normalized_key = str(key)
            normalized_item = _json_value_in_place(item)
            if normalized_key != key:
                del value[key]
            value[normalized_key] = normalized_item
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _json_value_in_place(item)
        return value
    if isinstance(value, tuple):
        return [_json_value_in_place(item) for item in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


class SnapshotJSONEncoder(DjangoJSONEncoder):
    def default(self, value):
        if is_dataclass(value):
            return asdict(value)
        return super().default(value)


def canonical_json(payload):
    return json.dumps(
        payload,
        cls=SnapshotJSONEncoder,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_checksum(payload):
    digest = hashlib.sha256()
    encoder = SnapshotJSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(payload):
        digest.update(chunk.encode())
    return digest.hexdigest()


def _ranking_source_data(project, periods, selected_dates=None, selected_configurations=None):
    filters = {"project": project}
    selected_by_engine = selected_dates if isinstance(selected_dates, dict) else None
    if selected_configurations:
        filters["snapshot_date__in"] = {
            day for item in selected_configurations.values() for day in item.get("dates", ())
        }
    elif selected_dates:
        filters["snapshot_date__in"] = (
            {d for dates in selected_by_engine.values() for d in dates}
            if selected_by_engine
            else selected_dates
        )
    else:
        filters["snapshot_date__range"] = (periods.three_months.start, periods.report.end)
    snapshots = (
        RankingSnapshot.objects.filter(**filters)
        .prefetch_related("positions")
        .order_by("snapshot_date", "search_engine", "region", "topvisor_configuration_id", "id")
    )
    result = []
    for item in snapshots:
        configuration_selection = (selected_configurations or {}).get(
            str(item.topvisor_configuration_id)
        )
        if selected_configurations and (
            not configuration_selection
            or item.snapshot_date not in configuration_selection.get("dates", ())
        ):
            continue
        if selected_by_engine and item.snapshot_date not in selected_by_engine.get(
            item.search_engine, ()
        ):
            continue
        result.append(
            {
                "id": str(item.id),
                "date": item.snapshot_date,
                "search_engine": item.search_engine,
                "region": item.region,
                "configuration_id": item.topvisor_configuration_id,
                "ranking_depth": item.ranking_depth,
                "depth_raw": item.depth_raw,
                "visibility": item.visibility,
                "visibility_raw": item.visibility_raw,
                "positions": [
                    {
                        "query": row.query,
                        "normalized_query": row.normalized_query,
                        "frequency": row.frequency,
                        "position": row.position_value,
                        "status": row.position_status,
                        "group": row.group_name,
                        "target_url": row.normalized_target_url,
                    }
                    for row in item.positions.order_by("normalized_query", "group_name", "id")
                ],
                "provenance": {
                    "method": item.depth_source,
                    "retrieved_at": item.retrieved_at,
                    "depth_retrieved_at": item.depth_retrieved_at,
                    "response_checksum": item.response_checksum,
                    "import_batch_id": str(item.import_batch_id) if item.import_batch_id else None,
                },
            }
        )
    return result


def _external_source_data(project, periods, selected_ids=None):
    filters = {"project": project}
    if selected_ids is not None:
        filters["id__in"] = selected_ids
    else:
        filters["period_start__range"] = (periods.three_months.start, periods.report.start)
    rows = (
        SourceSnapshot.objects.filter(**filters)
        .defer("payload")
        .prefetch_related("metrics")
        .order_by("source", "period_start", "period_end", "id")
    )
    return [
        {
            "id": str(row.id),
            "source": row.source,
            "source_key": row.source_key,
            "period_start": row.period_start,
            "period_end": row.period_end,
            # Detailed provider data is frozen once in calculated period_details.
            # Keeping a second copy here made large Metrika reports exceed the
            # web worker timeout and doubled the immutable JSON snapshot.
            "payload": {},
            "metrics": [
                {
                    "code": point.metric_code,
                    "value": point.numeric_value,
                    "unit": point.unit,
                    "dimensions": point.dimensions,
                }
                for point in row.metrics.order_by("metric_code", "id")
            ],
            "retrieval_method": row.retrieval_method,
            "checksum": row.checksum,
            "retrieved_at": row.retrieved_at,
            "provenance": redact_sensitive_source_data(row.provenance),
            "sampling": row.sampling,
            "contains_sensitive_data": row.contains_sensitive_data,
        }
        for row in rows
    ]


def build_report_snapshot(*, report, selection=None):
    project = report.project
    periods = calculate_periods(report.report_month)
    works = (
        WorkLogItem.objects.filter(
            project=project, work_date__range=(periods.report.start, periods.report.end)
        )
        .select_related("category")
        .order_by("work_date", "category__sort_order", "title", "id")
    )
    explicit_selection = selection is not None
    selection = selection or {}
    raw_topvisor = selection.get("topvisor")
    raw_configurations = selection.get("topvisor_configurations") or {}
    selected_configurations = {
        str(configuration): {
            "engine": str(item.get("engine") or ""),
            "dates": tuple(date.fromisoformat(value) for value in item.get("dates", ())),
        }
        for configuration, item in raw_configurations.items()
    }
    if raw_topvisor is None:
        legacy = selection.get("topvisor_dates", ())
        selected_dates = tuple(date.fromisoformat(value) for value in legacy)
        selected_by_engine = None
    else:
        selected_by_engine = {
            engine: tuple(date.fromisoformat(value) for value in raw_topvisor.get(engine, ()))
            for engine in ("yandex", "google")
        }
        selected_dates = selected_by_engine
    selected_source_ids = tuple(selection.get("yandex_metrika", ())) + tuple(
        selection.get("yandex_webmaster", ())
    )
    display_options = (
        selection.get("display_options", {"show_urls": False})
        if explicit_selection
        else {"show_urls": True}
    )
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "project": {
            "id": str(project.id),
            "name": project.name,
            "domain": project.domain,
            "normalized_domain": project.normalized_domain,
            "timezone": project.timezone,
            "language": project.language,
            "top_11_20_mode": project.top_11_20_mode,
            "position_provider": project.position_provider,
            "favicon": _project_favicon(project.normalized_domain),
            "brand_rules": list(
                project.brand_rules.order_by("kind", "pattern", "priority", "id").values(
                    "kind", "pattern", "priority", "active"
                )
            ),
            "url_groups": [
                {
                    "name": group.name,
                    "slug": group.slug,
                    "priority": group.priority,
                    "active": group.active,
                    "rules": list(
                        group.rules.order_by("type", "pattern", "priority", "id").values(
                            "type", "pattern", "priority", "active"
                        )
                    ),
                }
                for group in project.url_groups.prefetch_related("rules").order_by(
                    "name", "slug", "priority", "id"
                )
            ],
            "provenance": {"method": "project_database", "updated_at": project.updated_at},
        },
        "periods": periods,
        "display_options": display_options,
        "source_selection": {
            "topvisor": (
                {
                    engine: {
                        "selected_dates": dates,
                        "comparison_start": dates[0] if dates else None,
                        "comparison_end": dates[-1] if dates else None,
                        "intermediate_dates": dates[1:-1],
                        "snapshots": [],
                    }
                    for engine, dates in selected_by_engine.items()
                }
                if selected_by_engine is not None
                else {
                    "selected_dates": selected_dates,
                    "comparison_start": selected_dates[0] if selected_dates else None,
                    "comparison_end": selected_dates[-1] if selected_dates else None,
                    "intermediate_dates": selected_dates[1:-1],
                }
            ),
            "yandex_metrika": list(selection.get("yandex_metrika", ())),
            "yandex_webmaster": list(selection.get("yandex_webmaster", ())),
        },
        "ranking_sources": _ranking_source_data(
            project, periods, selected_dates, selected_configurations
        ),
        "source_snapshots": _external_source_data(
            project, periods, selected_source_ids if explicit_selection else None
        ),
        "calculated": {
            "positions": build_position_facts(
                project=project,
                report_month=report.report_month,
                selected_dates=selected_dates,
                selected_configurations=selected_configurations,
            ),
            "sources": build_source_facts(
                project=project,
                report_month=report.report_month,
                selected_snapshot_ids={
                    SourceSnapshot.Source.METRIKA: selection.get("yandex_metrika", []),
                    SourceSnapshot.Source.WEBMASTER: selection.get("yandex_webmaster", []),
                }
                if explicit_selection
                else None,
                display_options=display_options,
            ),
        },
        "completed_work": [
            {
                "date": item.work_date,
                "category": item.category.name,
                "title": item.title,
                "status": item.status,
                "url": item.url,
                "page_or_material_name": item.page_or_material_name,
                "character_count": item.character_count,
                "responsible": item.responsible,
                "comment": item.comment,
                "result_url": item.result_url,
                "provenance": {
                    "method": "worklog",
                    "id": str(item.id),
                    "updated_at": item.updated_at,
                },
            }
            for item in works
        ],
    }
    if selected_by_engine is not None:
        if selected_configurations:
            payload["source_selection"]["topvisor"]["configurations"] = {
                configuration: {
                    "engine": item["engine"],
                    "selected_dates": item["dates"],
                }
                for configuration, item in selected_configurations.items()
            }
        for engine in selected_by_engine:
            payload["source_selection"]["topvisor"][engine]["snapshots"] = [
                {
                    "identifier": source["id"],
                    "checksum": source["provenance"].get("response_checksum"),
                    "date": source["date"],
                    "search_engine": source["search_engine"],
                    "region": source["region"],
                    "configuration": source["configuration_id"],
                    "actual_depth": source["ranking_depth"],
                    "retrieved_at": source["provenance"].get("retrieved_at"),
                    "provenance": {"method": source["provenance"].get("method")},
                }
                for source in payload["ranking_sources"]
                if source["search_engine"] == engine
            ]
    return _json_value_in_place(payload)


@transaction.atomic
def create_report_version(*, report, created_by=None, selection=None):
    """Explicitly freeze current source data; no version is made by reads or source updates."""
    locked_report = Report.objects.select_for_update().select_related("project").get(pk=report.pk)
    number = (locked_report.versions.aggregate(value=Max("number"))["value"] or 0) + 1
    payload = build_report_snapshot(report=locked_report, selection=selection)
    version = ReportVersion.objects.create(
        report=locked_report, number=number, created_by=created_by
    )
    ReportDatasetSnapshot.objects.create(
        version=version,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        formula_version=FORMULA_VERSION,
        payload=payload,
        checksum=snapshot_checksum(payload),
    )
    issues = [
        ValidationIssue(
            version=version,
            code=warning["code"],
            section_code="position_dynamics",
            details=warning,
            message="Глубина проверки позиций изменилась относительно предыдущего месяца.",
        )
        for segment in payload["calculated"]["positions"]["segments"]
        for warning in segment["warnings"]
    ]
    ValidationIssue.objects.bulk_create(issues)
    from .narratives import generate_narratives

    generate_narratives(version, payload=payload)
    return version


@transaction.atomic
def delete_report_version(*, version):
    """Delete one explicitly selected version without touching live source snapshots."""
    locked_report = Report.objects.select_for_update().get(pk=version.report_id)
    locked_version = ReportVersion.objects.select_for_update().get(
        pk=version.pk, report=locked_report
    )
    active_after = timezone.now() - timedelta(seconds=settings.REPORT_ARTIFACT_STALE_SECONDS)
    if locked_version.generated_artifacts.filter(
        status=GeneratedArtifact.Status.GENERATING,
        created_at__gte=active_after,
    ).exists():
        raise ReportVersionDeleteBlocked(
            "Нельзя удалить версию, пока для неё формируется файл. Повторите после завершения."
        )

    artifact_files = [
        (artifact.file.storage, artifact.file.name)
        for artifact in locked_version.generated_artifacts.exclude(file="")
        if artifact.file.name
    ]
    # Normal snapshot deletion stays forbidden. This explicit workflow removes the
    # protected child first and then lets the version cascade delete its own rows.
    ReportDatasetSnapshot.objects.filter(version=locked_version).delete()
    locked_version.delete()
    for storage, name in artifact_files:
        transaction.on_commit(partial(_delete_artifact_file, storage, name))
    return locked_version.number


def get_report_version_data(version):
    """Read only the frozen row: deliberately has no source adapter calls."""
    return ReportDatasetSnapshot.objects.only("payload").get(version=version).payload
