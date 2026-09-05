"""Make automatic Topvisor visibility match the integer value shown by Topvisor."""

from copy import deepcopy
from decimal import ROUND_DOWN, Decimal, InvalidOperation

_APPLIED = False


def topvisor_display_visibility(value):
    """Topvisor shows visibility as an integer without mathematical rounding."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return number.quantize(Decimal("1"), rounding=ROUND_DOWN)


def _is_topvisor_payload(payload):
    provider = str((payload.get("project") or {}).get("position_provider") or "").casefold()
    return provider in {"", "topvisor"}


def _normalize_segment_visibility(segment):
    normalized = deepcopy(segment)
    for key in ("three_month_series", "chart_series"):
        series = normalized.get(key)
        if not series:
            continue
        for point in series:
            if point.get("visibility") is not None:
                point["visibility"] = topvisor_display_visibility(point.get("visibility"))
    return normalized


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from apps.projects.models import Project

    from . import exporting as exp
    from . import services, views

    original_build_position_facts = services.build_position_facts

    def build_position_facts(*args, **kwargs):
        facts = original_build_position_facts(*args, **kwargs)
        project = kwargs.get("project")
        if project is None and args:
            project = args[0]
        if not project or project.position_provider != Project.PositionProvider.TOPVISOR:
            return facts

        for segment in facts.get("segments", []):
            for key in ("three_month_series", "chart_series"):
                for point in segment.get(key) or []:
                    if point.get("visibility") is not None:
                        point["visibility"] = topvisor_display_visibility(point.get("visibility"))

            change = segment.get("visibility_change")
            if change is not None:
                current = topvisor_display_visibility(change.current)
                previous = topvisor_display_visibility(change.previous)
                segment["visibility_change"] = services.calculate_change(
                    current,
                    previous,
                    kind=services.ChangeKind.PERCENTAGE_POINTS,
                )
        return facts

    services.build_position_facts = build_position_facts

    original_editor_data = views._topvisor_editor_data

    def topvisor_editor_data(project):
        rows, segments = original_editor_data(project)
        if project.position_provider == Project.PositionProvider.TOPVISOR:
            for row in rows:
                if row.get("visibility") is not None:
                    row["visibility"] = float(topvisor_display_visibility(row.get("visibility")))
        return rows, segments

    views._topvisor_editor_data = topvisor_editor_data

    original_manual_segment = exp._manual_topvisor_segment

    def manual_topvisor_segment(payload, segment):
        source = (
            _normalize_segment_visibility(segment) if _is_topvisor_payload(payload) else segment
        )
        return original_manual_segment(payload, source)

    exp._manual_topvisor_segment = manual_topvisor_segment
    views._manual_topvisor_segment = manual_topvisor_segment
    exp.GENERATOR_VERSION = "mvp1.11-2026-09-05"
