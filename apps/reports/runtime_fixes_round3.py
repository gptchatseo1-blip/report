"""Make automatic Topvisor visibility display match the provider UI."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_APPLIED = False


def topvisor_display_visibility(value):
    """Render provider visibility to the same whole percent shown in Topvisor UI."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from apps.projects.models import Project

    from . import services, views

    original_build_position_facts = services.build_position_facts

    def build_position_facts(*args, **kwargs):
        facts = original_build_position_facts(*args, **kwargs)
        project = kwargs.get("project")
        if project is None and args:
            project = args[0]
        if not project or project.position_provider != Project.PositionProvider.TOPVISOR:
            return facts

        # Keep raw fractional visibility in graph series so the line geometry stays exact.
        # Only the user-facing summary values are converted to whole percents, as in Topvisor.
        for segment in facts.get("segments", []):
            change = segment.get("visibility_change")
            if change is None:
                continue
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
