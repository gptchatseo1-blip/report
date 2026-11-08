"""Repair the identifiable automatic TOP-percentage bug without changing manual rows."""

import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation

_COUNTS = ("top3", "top10", "top11_30")


def _number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _key(row):
    return (
        str(row.get("engine") or "").strip().casefold(),
        " ".join(str(row.get("region") or "").split()).casefold(),
        str(row.get("month") or "")[:7],
    )


def _repair_candidate(row):
    if not isinstance(row, dict) or not row.get("month"):
        return False
    total = _number(row.get("total"))
    counts = [_number(row.get(name)) for name in _COUNTS]
    if not total or any(count is None for count in counts):
        return False
    impossible_total = total < max(counts[0], counts[1] + counts[2])
    if not impossible_total and row.get("manual_override") is not False:
        return False
    # The browser capped the generated percentages at 100 when saving. Accept
    # both that exact signature and the original uncapped automatic values.
    shares = [_number(row.get(f"{name}_percent")) for name in _COUNTS]
    expected = [int(count * 100 / total + Decimal("0.5")) for count in counts]
    return shares == expected or shares == [min(100, share) for share in expected]


def repair_stale_topvisor_percentages(project, value):
    """Repair generated shares whose saved counts still match their source check.

    Selection, manual visibility, deletion markers and every unrelated setting
    are retained. Explicit overrides are repaired only for the unmistakable
    impossible-total signature. Nothing is written to the database here.
    """
    if project is None or getattr(project, "position_provider", "") != "topvisor":
        return value
    was_string = isinstance(value, str)
    try:
        rows = json.loads(value or "[]") if was_string else value
    except json.JSONDecodeError:
        return value
    if not isinstance(rows, list):
        return value
    candidates = [row for row in rows if _repair_candidate(row)]
    if not candidates:
        return value

    from .views import _topvisor_editor_data

    dates_by_configuration = defaultdict(lambda: {"dates": []})
    dates_by_engine = defaultdict(list)
    for row in candidates:
        day = str(row.get("snapshot_date") or "")[:10]
        if not day:
            continue
        configuration = str(row.get("configuration_id") or "")
        if configuration:
            dates_by_configuration[configuration]["dates"].append(day)
        else:
            dates_by_engine[str(row.get("engine") or "").casefold()].append(day)
    automatic, _segments = _topvisor_editor_data(
        project,
        selected_dates_by_engine=dates_by_engine,
        selected_dates_by_configuration=dates_by_configuration,
    )
    automatic_by_key = {_key(row): row for row in automatic}
    changed = False
    repaired = []
    for row in rows:
        source = automatic_by_key.get(_key(row)) if _repair_candidate(row) else None
        if source is None:
            repaired.append(row)
            continue
        if row.get("configuration_id") and row["configuration_id"] != source["configuration_id"]:
            repaired.append(row)
            continue
        if row.get("snapshot_date") and row["snapshot_date"] != source["snapshot_date"]:
            repaired.append(row)
            continue
        if any(_number(row.get(name)) != source[name] for name in _COUNTS):
            repaired.append(row)
            continue
        if source["total"] < max(source["top3"], source["top10"] + source["top11_30"]):
            repaired.append(row)
            continue
        corrected = {
            "total": source["total"],
            **{f"{name}_percent": source[f"{name}_percent"] for name in _COUNTS},
        }
        if all(_number(row.get(name)) == number for name, number in corrected.items()):
            repaired.append(row)
            continue
        repaired.append({**row, **corrected})
        changed = True
    if not changed:
        return value
    return json.dumps(repaired, ensure_ascii=False) if was_string else repaired
