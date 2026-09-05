"""Maintenance actions for mutable Topvisor dynamics editor data."""

import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
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


def _engine_key(value):
    normalized = _normalized(value)
    if "yandex" in normalized or "яндекс" in normalized:
        return "yandex"
    if "google" in normalized or "гугл" in normalized:
        return "google"
    return normalized


def _row_key(row):
    return (
        _engine_key(row.get("engine")),
        _normalized(row.get("region")),
        str(row.get("month") or "")[:7],
    )


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace("%", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


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


def _matching_snapshots(project, configuration_id_value, engine, region):
    from apps.metrics.models import RankingSnapshot

    exact = list(
        RankingSnapshot.objects.filter(
            project=project,
            topvisor_configuration_id=configuration_id_value,
        ).order_by("snapshot_date", "created_at", "id")
    )
    if exact:
        return exact

    engine_key = _engine_key(engine)
    region_key = _normalized(region)
    return [
        snapshot
        for snapshot in RankingSnapshot.objects.filter(project=project).order_by(
            "snapshot_date", "created_at", "id"
        )
        if _engine_key(snapshot.search_engine) == engine_key
        and _normalized(snapshot.region) == region_key
    ]


def refresh_provider_visibility(project, *, engine=None, region=None, client=None):
    """Refresh stored Topvisor visibility from the provider summary chart."""
    if project.position_provider != Project.PositionProvider.TOPVISOR:
        return 0

    from apps.topvisor.client import client_for_project
    from apps.topvisor.models import TopvisorProjectMapping
    from apps.topvisor.services import _summary_visibility, configuration_id, configuration_segment

    mapping = TopvisorProjectMapping.objects.filter(project=project).first()
    if mapping is None:
        return 0

    target = (_engine_key(engine), _normalized(region)) if engine else None
    api = client or client_for_project(project)[0]
    updated = 0
    retrieved_at = timezone.now()

    for configuration in mapping.selected_configurations:
        try:
            stable_id = configuration_id(configuration)
        except ValueError:
            continue
        config_engine, config_region = configuration_segment(configuration)
        region_label = str(
            configuration.get("region_name") or configuration.get("region") or ""
        ).strip()
        segment = (_engine_key(config_engine), _normalized(region_label or config_region))
        if target and segment != target:
            continue

        region_index = configuration.get("region_index")
        if region_index in (None, ""):
            continue
        provider_project_id = str(
            configuration.get("_topvisor_project_id") or mapping.topvisor_project_id
        )
        snapshots = _matching_snapshots(
            project,
            stable_id,
            config_engine,
            region_label or config_region,
        )
        if not snapshots:
            continue

        for start in range(0, len(snapshots), 31):
            batch = snapshots[start : start + 31]
            dates = [snapshot.snapshot_date.isoformat() for snapshot in batch]
            payload = api.get_summary_chart(
                provider_project_id,
                region_index=region_index,
                dates=dates,
            )
            values = _summary_visibility(payload, provider_project_id)
            changed = []
            for snapshot in batch:
                day = snapshot.snapshot_date.isoformat()
                exact = _decimal(values.get(day))
                if exact is None:
                    continue
                raw = {
                    "value": str(exact),
                    "source": "topvisor_api_summary_chart",
                    "retrieved_at": retrieved_at.isoformat(),
                }
                provenance = dict(snapshot.provenance or {})
                provenance["visibility"] = raw
                snapshot.visibility = exact
                snapshot.visibility_raw = raw
                snapshot.provenance = provenance
                snapshot.retrieved_at = retrieved_at
                changed.append(snapshot)
            if changed:
                from apps.metrics.models import RankingSnapshot

                RankingSnapshot.objects.bulk_update(
                    changed,
                    ["visibility", "visibility_raw", "provenance", "retrieved_at"],
                )
                updated += len(changed)
    return updated


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
    target = (_engine_key(engine), _normalized(region))
    automatic_by_key = {_row_key(row): row for row in _automatic_rows(project)}
    cleared = []

    for existing in saved_rows:
        segment = (
            _engine_key(existing.get("engine")),
            _normalized(existing.get("region")),
        )
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
    try:
        refreshed_count = refresh_provider_visibility(project)
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "message": "Не удалось обновить видимость из Topvisor. Повторите попытку.",
            },
            status=502,
        )
    rows = refresh_editor_rows(project)
    message = (
        f"Данные Topvisor обновлены ({refreshed_count} снимков)."
        if refreshed_count
        else "Данные обновлены."
    )
    return JsonResponse({"ok": True, "rows": rows, "message": message})


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
    try:
        refresh_provider_visibility(project, engine=engine, region=region)
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "message": "Не удалось получить актуальную видимость Topvisor. Очистка отменена.",
            },
            status=502,
        )
    rows = clear_editor_segment(project, engine, region)
    return JsonResponse(
        {
            "ok": True,
            "rows": rows,
            "message": "Ручные данные очищены, видимость обновлена из Topvisor.",
        }
    )
