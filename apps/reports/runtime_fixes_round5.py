"""Repair stale automatic Topvisor visibility values captured as manual overrides."""

import json
from decimal import ROUND_DOWN, Decimal, InvalidOperation

_APPLIED = False
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


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace("%", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _same_number(left, right):
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is None or right_number is None:
        return left_number is right_number
    return abs(left_number - right_number) <= Decimal("0.000001")


def _automatic_row(snapshot):
    positions = list(snapshot.positions.all())
    ranked = [item.position_value for item in positions if item.position_value is not None]
    total = len(positions)
    top3 = sum(value <= 3 for value in ranked)
    top10 = sum(value <= 10 for value in ranked)
    top11_30 = sum(11 <= value <= min(snapshot.ranking_depth, 30) for value in ranked)
    return {
        "total": total,
        "top3": top3,
        "top10": top10,
        "top11_30": top11_30,
        "top3_percent": round(top3 * 100 / total) if total else 0,
        "top10_percent": round(top10 * 100 / total) if total else 0,
        "top11_30_percent": round(top11_30 * 100 / total) if total else 0,
    }


def _snapshot_maps(project):
    from apps.metrics.models import RankingSnapshot

    exact = {}
    fallback = {}
    snapshots = (
        RankingSnapshot.objects.filter(project=project)
        .prefetch_related("positions")
        .order_by("snapshot_date", "created_at", "id")
    )
    for snapshot in snapshots:
        month = snapshot.snapshot_date.isoformat()[:7]
        engine = _normalized(snapshot.search_engine)
        region = _normalized(snapshot.region)
        configuration = str(snapshot.topvisor_configuration_id or "")
        exact[(engine, region, configuration, month)] = snapshot
        fallback[(engine, region, month)] = snapshot
    return exact, fallback


def sanitize_stale_topvisor_visibility(project, value):
    """Remove only the known floor-rounded automatic value that was stored as manual."""
    if project is None or getattr(project, "position_provider", "") != "topvisor":
        return value

    was_string = isinstance(value, str)
    try:
        rows = json.loads(value or "[]") if was_string else value
    except json.JSONDecodeError:
        return value
    if not isinstance(rows, list) or not rows:
        return value

    from .runtime_fixes_round3 import topvisor_display_visibility

    exact_map, fallback_map = _snapshot_maps(project)
    changed = False
    result = []
    for source in rows:
        if not isinstance(source, dict) or not source.get("month"):
            result.append(source)
            continue
        row = dict(source)
        key = (
            _normalized(row.get("engine")),
            _normalized(row.get("region")),
            str(row.get("configuration_id") or ""),
            str(row.get("month"))[:7],
        )
        snapshot = exact_map.get(key) or fallback_map.get((key[0], key[1], key[3]))
        if snapshot is None or snapshot.visibility is None:
            result.append(row)
            continue

        exact_visibility = Decimal(str(snapshot.visibility))
        current_display = topvisor_display_visibility(exact_visibility)
        old_floor_display = exact_visibility.quantize(Decimal("1"), rounding=ROUND_DOWN)
        stored_visibility = _decimal(row.get("visibility"))
        automatic_marker = _decimal(row.get("automatic_visibility"))
        has_explicit_override_flag = "manual_override" in row

        # The production bug stored the old floor-rounded automatic value (15 for
        # 15.65) in the manual field. A row is migrated only when it has the
        # characteristic automatic signature: legacy row without an override flag,
        # or a newer row whose automatic marker already equals the corrected value.
        stale_floor = (
            stored_visibility is not None
            and old_floor_display != current_display
            and stored_visibility == old_floor_display
            and (
                not has_explicit_override_flag
                or automatic_marker == current_display
                or row.get("manual_override") is False
            )
        )
        stale_exact_legacy = (
            stored_visibility is not None
            and not has_explicit_override_flag
            and exact_visibility != current_display
            and stored_visibility == exact_visibility
        )
        if not (stale_floor or stale_exact_legacy):
            result.append(row)
            continue

        automatic = _automatic_row(snapshot)
        top_changed = any(
            not _same_number(row.get(name, 0), automatic[name]) for name in _TOP_FIELDS
        )
        row["visibility"] = None
        row["automatic_visibility"] = float(current_display)
        row["manual_override"] = bool(top_changed)
        changed = True
        result.append(row)

    if not changed:
        return value
    return json.dumps(result, ensure_ascii=False) if was_string else result


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import forms as report_forms

    original_init = report_forms.ReportCreateForm.__init__
    original_clean = report_forms.ReportCreateForm.clean

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        if self.project is None or self.is_bound:
            return
        value = self.initial.get("topvisor_manual_rows")
        if value:
            self.initial["topvisor_manual_rows"] = sanitize_stale_topvisor_visibility(
                self.project, value
            )

    def patched_clean(self):
        cleaned = original_clean(self)
        if self.project is not None and "topvisor_manual_rows" in cleaned:
            cleaned["topvisor_manual_rows"] = sanitize_stale_topvisor_visibility(
                self.project, cleaned.get("topvisor_manual_rows")
            )
        return cleaned

    report_forms.ReportCreateForm.__init__ = patched_init
    report_forms.ReportCreateForm.clean = patched_clean
