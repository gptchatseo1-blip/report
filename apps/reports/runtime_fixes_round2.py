"""Second targeted report-polish package agreed on 2026-09-05."""

import json
import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError

_APPLIED = False


def _number(raw, *, maximum, integer=False, label="значение", allow_none=False):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_none:
            return None
        raw = 0
    rendered = str(raw).strip().replace("%", "").replace(" ", "").replace(",", ".")
    try:
        result = float(rendered)
    except (TypeError, ValueError):
        raise ValidationError(f"Некорректное поле «{label}» в ручной строке.") from None
    if result < 0 or result > maximum or (integer and not result.is_integer()):
        raise ValidationError(f"Некорректное поле «{label}» в ручной строке.")
    return int(round(result)) if integer else result


def _bool(raw, default=False):
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(raw)


def validate_manual_rows(value):
    """Validate editor rows, including per-month inclusion and deletion tombstones."""
    try:
        rows = json.loads(value or "[]") if isinstance(value, str) else value
    except json.JSONDecodeError:
        raise ValidationError("Некорректные ручные значения Topvisor.") from None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValidationError("Некорректные ручные значения Topvisor.")

    cleaned = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("Некорректная ручная строка динамики.")
        engine = str(row.get("engine") or "").casefold()[:16]
        region = str(row.get("region") or "").strip()[:120]
        month = str(row.get("month") or "")[:7]
        try:
            month = date.fromisoformat(f"{month}-01").isoformat() if month else ""
        except ValueError:
            raise ValidationError("Укажите корректный месяц в ручной строке.") from None
        if not month:
            raise ValidationError("Месяц в ручной строке обязателен.")
        if not engine:
            raise ValidationError("Поисковая система в ручной строке обязательна.")
        key = (engine, " ".join(region.split()).casefold(), month[:7])
        if key in seen:
            raise ValidationError(
                "Для одной поисковой системы и региона месяц не должен повторяться."
            )
        seen.add(key)
        include_explicit = "include_in_report" in row
        manual_override_explicit = "manual_override" in row
        cleaned.append(
            {
                "configuration_id": str(row.get("configuration_id") or "")[:120],
                "engine": engine,
                "region": region,
                "month": month,
                "include_in_report": _bool(row.get("include_in_report"), True),
                "include_explicit": include_explicit,
                "deleted": _bool(row.get("deleted"), False),
                "manual_override": _bool(
                    row.get("manual_override"),
                    True if not manual_override_explicit else False,
                ),
                "visibility": _number(
                    row.get("visibility"),
                    maximum=100,
                    label="видимость",
                    allow_none=True,
                ),
                "automatic_visibility": _number(
                    row.get("automatic_visibility"),
                    maximum=100,
                    label="автоматическая видимость",
                    allow_none=True,
                ),
                "total": _number(
                    row.get("total", 0),
                    maximum=10_000_000,
                    integer=True,
                    label="всего",
                ),
                "top3": _number(
                    row.get("top3", 0),
                    maximum=10_000_000,
                    integer=True,
                    label="в топ 3",
                ),
                "top10": _number(
                    row.get("top10", 0),
                    maximum=10_000_000,
                    integer=True,
                    label="в топ 10",
                ),
                "top11_30": _number(
                    row.get("top11_30", 0),
                    maximum=10_000_000,
                    integer=True,
                    label="в топ 11–30",
                ),
                "top3_percent": _number(
                    row.get("top3_percent", 0),
                    maximum=100,
                    label="процент в топ 3",
                ),
                "top10_percent": _number(
                    row.get("top10_percent", 0),
                    maximum=100,
                    label="процент в топ 10",
                ),
                "top11_30_percent": _number(
                    row.get("top11_30_percent", 0),
                    maximum=100,
                    label="процент в топ 11–30",
                ),
            }
        )
    return sorted(
        cleaned,
        key=lambda row: (row["engine"], row["region"].casefold(), row["month"]),
    )


def _normalized(value):
    return " ".join(str(value or "").split()).casefold()


def _row_distribution(row, depth):
    top3 = max(int(row.get("top3") or 0), 0)
    top10 = max(int(row.get("top10") or 0), top3)
    top11 = max(int(row.get("top11_30") or 0), 0)
    final_label = "11-30" if depth >= 30 else "11-20"
    shares = {
        "1-3": max(0, min(100, float(row.get("top3_percent") or 0))),
        "1-10": max(0, min(100, float(row.get("top10_percent") or 0))),
        final_label: max(0, min(100, float(row.get("top11_30_percent") or 0))),
    }
    inferred_totals = [
        round(count * 100 / shares[label])
        for label, count in (("1-3", top3), ("1-10", top10))
        if shares[label] > 0
    ]
    total = max(
        int(row.get("total") or 0),
        *(inferred_totals or [0]),
        top10 + top11,
    )
    ranges = {
        "1-3": top3,
        "4-10": max(top10 - top3, 0),
        "11-20": top11,
    }
    if depth >= 30:
        ranges["21-30"] = 0
    return {
        "total": total,
        "top_10": top10,
        "ranges": ranges,
        "manual_buckets": {
            "1-3": {"count": top3, "share": shares["1-3"]},
            "1-10": {"count": top10, "share": shares["1-10"]},
            final_label: {"count": top11, "share": shares[final_label]},
        },
    }


def _manual_topvisor_segment(payload, segment):
    """Apply active manual rows and expose a dedicated monthly-table series."""
    rows = payload.get("display_options", {}).get("topvisor_manual_rows") or []
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except json.JSONDecodeError:
            rows = []
    segment_engine = _normalized(segment.get("search_engine"))
    segment_region = _normalized(segment.get("region"))
    segment_configuration = str(segment.get("configuration_id") or "")
    selected = [
        row
        for row in rows
        if row.get("month")
        and (
            not row.get("configuration_id")
            or str(row.get("configuration_id")) == segment_configuration
        )
        and (not row.get("engine") or _normalized(row.get("engine")) == segment_engine)
        and (not row.get("region") or _normalized(row.get("region")) == segment_region)
    ]
    if not selected:
        return segment

    depth = segment.get("ranking_depth") or 30
    history_by_month = {
        str(point.get("month"))[:7]: dict(point)
        for point in segment.get("three_month_series") or []
        if point.get("month")
    }
    explicit_selection = any(
        row.get("include_explicit") or "include_in_report" in row for row in selected
    )
    monthly_by_month = {}

    for row in sorted(selected, key=lambda item: item["month"]):
        if row.get("deleted"):
            continue
        month_key = str(row["month"])[:7]
        existing = history_by_month.get(month_key)
        include = row.get("include_in_report", True)
        should_apply = not explicit_selection or include
        if should_apply and (row.get("manual_override", True) or existing is None):
            manual_visibility = row.get("visibility")
            fallback_visibility = (
                existing.get("visibility") if existing else row.get("automatic_visibility")
            )
            history_by_month[month_key] = {
                **(existing or {}),
                "month": row["month"],
                "visibility": (
                    manual_visibility
                    if manual_visibility is not None
                    else fallback_visibility
                ),
                "distribution": _row_distribution(row, depth),
                "ranking_depth": (existing or {}).get("ranking_depth", depth),
                "manual_override": bool(row.get("manual_override", True)),
            }
        point = history_by_month.get(month_key)
        if include and point is not None:
            monthly_by_month[month_key] = dict(point)

    history = [history_by_month[key] for key in sorted(history_by_month)]
    if not history:
        return segment
    monthly_history = (
        [monthly_by_month[key] for key in sorted(monthly_by_month)]
        if explicit_selection
        else history
    )
    return {
        **segment,
        "three_month_series": history,
        "chart_series": history,
        "monthly_table_series": monthly_history,
        "distribution": history[-1].get("distribution") or segment.get("distribution") or {},
    }


def _display_percent(value):
    number = Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(number)


def _visibility_chart(exp, points, title=None):
    useful = [(month, value) for month, value in points if value is not None]
    if not useful:
        return None
    with exp.plt.rc_context({"font.family": exp.CHART_FONT, "font.size": 9}):
        figure = exp.plt.figure(figsize=(7.2, 3.0), dpi=150, facecolor="white")
        grid = figure.add_gridspec(1, 2, width_ratios=(3.9, 1.55), wspace=0.08)
        axis = figure.add_subplot(grid[0, 0])
        labels = [exp._date_label(month) for month, _value in useful]
        values = [float(value) for _month, value in useful]
        x_values = list(range(len(values)))
        green = exp.TOPVISOR_COLORS["visibility"]
        exp._plot_smooth_line(axis, x_values, values, color=green, linewidth=1.8)
        axis.scatter(x_values, values, color=green, s=13, zorder=3)
        axis.fill_between(x_values, values, color=green, alpha=0.07)
        ticks, tick_labels = exp._date_ticks(labels)
        axis.set_xticks(ticks, tick_labels)
        if max(values) < 50:
            axis.set_ylim(0, 50)
            axis.set_yticks((0, 25, 50), labels=("0%", "25%", "50%"))
        else:
            axis.set_ylim(0, 100)
            axis.set_yticks((0, 50, 100), labels=("0%", "50%", "100%"))
        exp._style_axis(axis)

        donut = figure.add_subplot(grid[0, 1])
        current = max(0, min(100, values[-1]))
        donut.pie(
            [current, 100 - current],
            startangle=90,
            counterclock=False,
            radius=0.98,
            colors=(green, "#DCE0E5"),
            wedgeprops={"width": 0.44, "edgecolor": "none", "linewidth": 0},
        )
        donut.text(
            0,
            0,
            f"{_display_percent(current)}%",
            ha="center",
            va="center",
            fontsize=12,
            color=green,
        )
        donut.set_aspect("equal")
        donut.set_axis_off()
        figure.subplots_adjust(left=0.08, right=0.995, top=0.98, bottom=0.16)
        return exp._save_figure(figure)


def _render_distribution_cards_table(
    exp,
    doc,
    distribution,
    depth,
    *,
    engine="yandex",
):
    buckets = exp._topvisor_buckets(distribution, 20 if engine == "google" else depth)
    if engine == "google":
        buckets = [bucket for bucket in buckets if bucket["label"] in {"1-3", "1-10", "11-20"}]
    if not buckets:
        return None

    columns = 1 if engine == "google" else 2
    row_count = math.ceil(len(buckets) / columns)
    table = doc.add_table(rows=row_count, cols=columns)
    table.autofit = False
    outer_width = 5.0 if columns == 2 else 5.2
    for row in table.rows:
        exp._prevent_row_split(row)
        for cell in row.cells:
            exp._set_cell_width(cell, outer_width)
            exp._set_cell_margins(cell, top=18, bottom=18, left=35, right=35)
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = ""

    for index, bucket in enumerate(buckets):
        column = index // row_count
        row_index = index % row_count
        outer = table.rows[row_index].cells[column]
        nested = outer.add_table(rows=1, cols=3)
        nested.autofit = False
        widths = (1.65, 1.2, 1.8)
        for grid_column, width in zip(nested._tbl.tblGrid.gridCol_lst, widths, strict=True):
            grid_column.w = exp.Cm(width)
        label_cell, share_cell, count_cell = nested.rows[0].cells
        for cell, width in zip((label_cell, share_cell, count_cell), widths, strict=True):
            exp._set_cell_width(cell, width)
            exp._set_cell_margins(cell, top=12, bottom=12, left=20, right=20)
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
        exp._shade_cell(label_cell, exp.TOPVISOR_COLORS[bucket["label"]])
        label_cell.text = str(bucket["label"])
        share_cell.text = exp._number(bucket.get("share"), "%", decimal_places=0)
        count_cell.text = exp._number(bucket.get("count"), decimal_places=0)
        for paragraph in label_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="FFFFFF", bold=True)
        for paragraph in share_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="8491A5")
        for paragraph in count_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="3D4655")
        exp._set_table_borders(nested, "F0F2F5", size="0")
        for paragraph in outer.paragraphs:
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)

    if len(buckets) % columns:
        # Do not render a shaded placeholder when the final cell has no bucket.
        empty_column = columns - 1
        empty_cell = table.rows[-1].cells[empty_column]
        exp._shade_cell(empty_cell, "FFFFFF")
        exp._set_cell_margins(empty_cell, top=0, bottom=0, left=0, right=0)
    exp._set_table_borders(table, "FFFFFF", size="8")
    exp._keep_small_table_together(table)
    gap = doc.add_paragraph()
    gap.paragraph_format.space_after = exp.Pt(1)
    return table


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import exporting as exp
    from . import forms as report_forms
    from . import views

    report_forms.validate_topvisor_manual_rows = validate_manual_rows
    views._validated_manual_rows = validate_manual_rows
    views._manual_topvisor_segment = _manual_topvisor_segment

    original_monthly_table = exp._render_monthly_topvisor_table

    def render_monthly_table(doc, segment, *, show_visibility=True):
        series = segment.get("monthly_table_series")
        if series is None:
            return original_monthly_table(doc, segment, show_visibility=show_visibility)
        return original_monthly_table(
            doc,
            {**segment, "three_month_series": series},
            show_visibility=show_visibility,
        )

    exp.GENERATOR_VERSION = "mvp1.10-2026-09-05"
    exp._manual_topvisor_segment = _manual_topvisor_segment
    exp._visibility_chart = lambda points, title=None: _visibility_chart(exp, points, title)
    exp._render_distribution_cards_table = lambda doc, distribution, depth, *, engine="yandex": (
        _render_distribution_cards_table(exp, doc, distribution, depth, engine=engine)
    )
    exp._render_monthly_topvisor_table = render_monthly_table
