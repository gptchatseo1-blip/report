"""Maintenance actions for mutable Topvisor dynamics editor data."""

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from apps.projects.models import Project

from .models import ProjectReportSettings
from .runtime_fixes_round5 import sanitize_stale_topvisor_visibility

_TOP_FIELDS = (
    "total",
    "top3",
    "top10",
    "top11_30",
    "top3_percent",
    "top10_percent",
    "top11_30_percent",
)


def _normalized(value):
    return " ".join(str(value or "").split()).casefold()


def _row_key(row):
    return (
        _normalized(row.get("engine")),
        _normalized(row.get("region")),
        str(row.get("month") or "")[:7],
    )


def _read_saved_rows(project):
    settings = ProjectReportSettings.objects.filter(project=project).first()
    values = (settings.values if settings else {}) or {}
    raw = values.get("topvisor_manual_rows") or "[]"
    raw = sanitize_stale_topvisor_visibility(project, raw)
    try:
        rows = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        rows = []
    return settings, values, [dict(row) for row in rows if isinstance(row, dict)]


def _automatic_rows(project):
    from . import views

    rows, _segments = views._topvisor_editor_data(project)
    return [dict(row) for row in rows]


def _automatic_saved_row(automatic, existing=None, *, reset=False):
    existing = existing or {}
    result = {
        "configuration_id": str(automatic.get("configuration_id") or ""),
        "engine": str(automatic.get("engine") or "").casefold(),
        "region": str(automatic.get("region") or "").strip(),
        "month": str(automatic.get("month") or ""),
        "include_in_report": bool(existing.get("include_in_report", True)),
        "deleted": False if reset else bool(existing.get("deleted", False)),
        "manual_override": False,
        "visibility": None,
        "automatic_visibility": automatic.get("visibility"),
    }
    for name in _TOP_FIELDS:
        result[name] = automatic.get(name, 0)
    return result


def _save_rows(project, settings, values, rows):
    values = dict(values)
    values["topvisor_manual_rows"] = json.dumps(rows, ensure_ascii=False)
    if settings is None:
        ProjectReportSettings.objects.create(project=project, values=values)
    else:
        settings.values = values
        settings.save(update_fields=["values", "updated_at"])


def refresh_editor_rows(project):
    """Refresh automatic values while preserving explicit manual corrections."""
    settings, values, saved_rows = _read_saved_rows(project)
    automatic_by_key = {_row_key(row): row for row in _automatic_rows(project)}
    refreshed = []

    for existing in saved_rows:
        automatic = automatic_by_key.get(_row_key(existing))
        if automatic is None:
            # A manually added row has no automatic counterpart and must survive refresh.
            refreshed.append(existing)
            continue

        if existing.get("manual_override") is True:
            # Preserve deliberate edits, but move the automatic marker to the latest value.
            row = dict(existing)
            row["automatic_visibility"] = automatic.get("visibility")
            row["configuration_id"] = str(
                automatic.get("configuration_id") or row.get("configuration_id") or ""
            )
            refreshed.append(row)
            continue

        refreshed.append(_automatic_saved_row(automatic, existing))

    _save_rows(project, settings, values, refreshed)
    return refreshed


def clear_editor_segment(project, engine, region):
    """Reset one search-engine/region table to automatic data and remove manual-only rows."""
    settings, values, saved_rows = _read_saved_rows(project)
    target = (_normalized(engine), _normalized(region))
    automatic_by_key = {_row_key(row): row for row in _automatic_rows(project)}
    cleared = []

    for existing in saved_rows:
        segment = (_normalized(existing.get("engine")), _normalized(existing.get("region")))
        if segment != target:
            cleared.append(existing)
            continue

        automatic = automatic_by_key.get(_row_key(existing))
        if automatic is None:
            # Manual-only rows belong to the cleared segment and are intentionally removed.
            continue
        cleared.append(_automatic_saved_row(automatic, existing, reset=True))

    _save_rows(project, settings, values, cleared)
    return cleared


@login_required
@require_POST
def topvisor_editor_refresh(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    rows = refresh_editor_rows(project)
    return JsonResponse({"ok": True, "rows": rows, "message": "Данные обновлены."})


@login_required
@require_POST
def topvisor_editor_clear(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse(
            {"ok": False, "message": "Некорректные параметры очистки."},
            status=400,
        )
    engine = str(payload.get("engine") or "")[:32]
    region = str(payload.get("region") or "")[:200]
    if not engine:
        return JsonResponse(
            {"ok": False, "message": "Не указана поисковая система."},
            status=400,
        )
    rows = clear_editor_segment(project, engine, region)
    return JsonResponse({"ok": True, "rows": rows, "message": "Ручные данные очищены."})
