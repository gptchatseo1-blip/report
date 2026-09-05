"""Final dynamics-editor UX and Topvisor visibility fixes for 2026-09-05."""

from decimal import Decimal, InvalidOperation

from django.db.models import Q

_APPLIED = False

# fmt: off


def _normalized(value):
    return " ".join(str(value or "").split()).casefold()


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace("%", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def provider_visibility(snapshot):
    """Prefer the exact provider value retained in raw Topvisor payloads."""
    raw = getattr(snapshot, "visibility_raw", None)
    candidates = []
    if isinstance(raw, dict):
        candidates.extend((raw.get("value"), raw.get("visibility")))
    else:
        candidates.append(raw)

    provenance = getattr(snapshot, "provenance", None) or {}
    provenance_visibility = provenance.get("visibility")
    if isinstance(provenance_visibility, dict):
        candidates.extend(
            (
                provenance_visibility.get("value"),
                provenance_visibility.get("visibility"),
            )
        )
    else:
        candidates.append(provenance_visibility)

    candidates.append(getattr(snapshot, "visibility", None))
    for candidate in candidates:
        value = _decimal(candidate)
        if value is not None:
            return value
    return None


def _snapshot_maps(project, *, active_ids=None):
    from apps.metrics.models import RankingSnapshot

    snapshots = RankingSnapshot.objects.filter(project=project)
    if active_ids:
        snapshots = snapshots.filter(
            Q(topvisor_configuration_id__in=active_ids) | Q(topvisor_configuration_id="")
        )
    snapshots = snapshots.order_by("snapshot_date", "created_at", "id")

    exact_day = {}
    fallback_day = {}
    exact_month = {}
    fallback_month = {}
    for snapshot in snapshots:
        engine = _normalized(snapshot.search_engine)
        region = _normalized(snapshot.region)
        configuration = str(snapshot.topvisor_configuration_id or "")
        day = snapshot.snapshot_date.isoformat()
        month = day[:7]
        exact_day[(engine, region, configuration, day)] = snapshot
        fallback_day[(engine, region, day)] = snapshot
        exact_month[(engine, region, configuration, month)] = snapshot
        fallback_month[(engine, region, month)] = snapshot
    return exact_day, fallback_day, exact_month, fallback_month


def _find_snapshot(maps, row, *, exact_date=False):
    exact_day, fallback_day, exact_month, fallback_month = maps
    engine = _normalized(row.get("engine") or row.get("search_engine"))
    region = _normalized(row.get("region"))
    configuration = str(row.get("configuration_id") or "")
    raw_date = str(row.get("month") or "")
    if exact_date:
        day = raw_date[:10]
        return exact_day.get((engine, region, configuration, day)) or fallback_day.get(
            (engine, region, day)
        )
    month = raw_date[:7]
    return exact_month.get((engine, region, configuration, month)) or fallback_month.get(
        (engine, region, month)
    )


def _repair_editor_rows(project, rows):
    from apps.projects.models import Project

    if project.position_provider != Project.PositionProvider.TOPVISOR:
        return rows

    from .runtime_fixes_round3 import topvisor_display_visibility

    maps = _snapshot_maps(project)
    for row in rows:
        snapshot = _find_snapshot(maps, row)
        if snapshot is None:
            continue
        exact = provider_visibility(snapshot)
        if exact is None:
            continue
        row["visibility"] = float(topvisor_display_visibility(exact))
    return rows


def _repair_position_facts(project, facts, *, selected_dates=None):
    from apps.projects.models import Project

    if project.position_provider != Project.PositionProvider.TOPVISOR:
        return facts

    from . import services
    from .runtime_fixes_round3 import topvisor_display_visibility

    maps = _snapshot_maps(project)
    for segment in facts.get("segments", []):
        locator = {
            "engine": segment.get("search_engine"),
            "region": segment.get("region"),
            "configuration_id": segment.get("configuration_id"),
        }

        series_specs = (
            ("three_month_series", bool(selected_dates)),
            ("chart_series", True),
        )
        for series_name, exact_date in series_specs:
            repaired = []
            for point in segment.get(series_name) or []:
                source = {**locator, "month": point.get("month")}
                snapshot = _find_snapshot(maps, source, exact_date=exact_date)
                exact = provider_visibility(snapshot) if snapshot is not None else None
                visibility = exact if exact is not None else point.get("visibility")
                repaired.append({**point, "visibility": visibility})
            if repaired:
                segment[series_name] = tuple(repaired)

        history = list(segment.get("three_month_series") or [])
        previous = None
        current = None
        if selected_dates and history:
            previous = history[0].get("visibility")
            current = history[-1].get("visibility")
        elif history:
            periods = facts.get("periods")
            previous_month = getattr(getattr(periods, "previous", None), "start", None)
            report_month = getattr(getattr(periods, "report", None), "start", None)
            for point in history:
                point_month = str(point.get("month") or "")[:7]
                if previous_month and point_month == previous_month.isoformat()[:7]:
                    previous = point.get("visibility")
                if report_month and point_month == report_month.isoformat()[:7]:
                    current = point.get("visibility")
        if current is not None:
            segment["visibility_change"] = services.calculate_change(
                topvisor_display_visibility(current),
                topvisor_display_visibility(previous),
                kind=services.ChangeKind.PERCENTAGE_POINTS,
            )
    return facts


def _follow_monthly_table_toggle(cleaned):
    if "include_monthly_dynamics" in cleaned:
        cleaned["include_monthly_dynamics_table"] = bool(
            cleaned.get("include_monthly_dynamics")
        )
    return cleaned


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import forms as report_forms
    from . import services, views

    original_editor_data = views._topvisor_editor_data

    def topvisor_editor_data(project):
        rows, segments = original_editor_data(project)
        return _repair_editor_rows(project, rows), segments

    views._topvisor_editor_data = topvisor_editor_data

    original_build_position_facts = services.build_position_facts

    def build_position_facts(*args, **kwargs):
        facts = original_build_position_facts(*args, **kwargs)
        project = kwargs.get("project")
        if project is None and args:
            project = args[0]
        if project is None:
            return facts
        return _repair_position_facts(
            project,
            facts,
            selected_dates=kwargs.get("selected_dates"),
        )

    services.build_position_facts = build_position_facts

    original_clean = report_forms.ReportCreateForm.clean

    def patched_clean(self):
        return _follow_monthly_table_toggle(original_clean(self))

    report_forms.ReportCreateForm.clean = patched_clean


# fmt: on
