"""Targeted report-builder and export fixes agreed on 2026-09-05."""

import json
import math
from datetime import date
from decimal import Decimal

from django import forms as django_forms
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


def validate_manual_rows(value):
    """Validate dynamics and distinguish missing visibility from a real 0%."""
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
        cleaned.append(
            {
                "configuration_id": str(row.get("configuration_id") or "")[:120],
                "engine": engine,
                "region": region,
                "month": month,
                "visibility": _number(
                    row.get("visibility"),
                    maximum=100,
                    label="видимость",
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


def _manual_topvisor_segment(payload, segment):
    """Apply manual distribution and visibility without mutating RankingSnapshot."""
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
    for row in sorted(selected, key=lambda item: item["month"]):
        top3 = max(int(row.get("top3") or 0), 0)
        top10 = max(int(row.get("top10") or 0), top3)
        top11 = max(int(row.get("top11_30") or 0), 0)
        final_label = "11-30" if depth >= 30 else "11-20"
        shares = {
            "1-3": max(0, min(100, float(row.get("top3_percent") or 0))),
            "1-10": max(0, min(100, float(row.get("top10_percent") or 0))),
            final_label: max(
                0,
                min(100, float(row.get("top11_30_percent") or 0)),
            ),
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
        distribution = {
            "total": total,
            "top_10": top10,
            "ranges": ranges,
            "manual_buckets": {
                "1-3": {"count": top3, "share": shares["1-3"]},
                "1-10": {"count": top10, "share": shares["1-10"]},
                final_label: {"count": top11, "share": shares[final_label]},
            },
        }
        month_key = str(row["month"])[:7]
        existing = history_by_month.get(month_key) or {}
        manual_visibility = row.get("visibility")
        history_by_month[month_key] = {
            **existing,
            "month": row["month"],
            "visibility": (
                manual_visibility if manual_visibility is not None else existing.get("visibility")
            ),
            "distribution": distribution,
            "ranking_depth": existing.get("ranking_depth", depth),
            "manual_override": True,
        }

    history = [history_by_month[key] for key in sorted(history_by_month)]
    if not history:
        return segment
    return {
        **segment,
        "three_month_series": history,
        "chart_series": history,
        "distribution": history[-1].get("distribution") or segment.get("distribution") or {},
    }


def _visibility_chart(exp, points, title=None):
    useful = [(month, value) for month, value in points if value is not None]
    if not useful:
        return None
    with exp.plt.rc_context({"font.family": exp.CHART_FONT, "font.size": 9}):
        figure = exp.plt.figure(figsize=(7.2, 3.0), dpi=150, facecolor="white")
        grid = figure.add_gridspec(1, 2, width_ratios=(4.2, 0.9), wspace=0.16)
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
            radius=0.78,
            colors=(green, "#DCE0E5"),
            wedgeprops={"width": 0.35, "edgecolor": "none", "linewidth": 0},
        )
        donut.text(
            0,
            0,
            f"{current:.0f}%",
            ha="center",
            va="center",
            fontsize=10,
            color=green,
        )
        donut.set_aspect("equal")
        donut.set_axis_off()
        figure.subplots_adjust(left=0.08, right=0.99, top=0.98, bottom=0.16)
        return exp._save_figure(figure)


def _distribution_chart(exp, history, depth):
    useful_rows = [row for row in history if (row.get("distribution") or {}).get("total")]
    if not useful_rows:
        return None
    bucket_rows = [
        exp._topvisor_buckets(row.get("distribution") or {}, depth) for row in useful_rows
    ]
    labels = [exp._date_label(row.get("month")) for row in useful_rows]
    x_values = list(range(len(labels)))
    with exp.plt.rc_context({"font.family": exp.CHART_FONT, "font.size": 9}):
        figure, axis = exp.plt.subplots(
            figsize=(7.2, 3.35),
            dpi=150,
            facecolor="white",
        )
        bucket_names = [bucket["label"] for bucket in bucket_rows[0]]
        rows_by_name = [{bucket["label"]: bucket for bucket in buckets} for buckets in bucket_rows]
        handles = []
        for name in bucket_names:
            values = [float(row.get(name, {}).get("share") or 0) for row in rows_by_name]
            color = exp.TOPVISOR_COLORS[name]
            exp._plot_smooth_line(
                axis,
                x_values,
                values,
                color=color,
                linewidth=1.7,
            )
            axis.scatter(x_values, values, color=color, s=12, zorder=3)
            axis.fill_between(x_values, values, color=color, alpha=0.055)
            handles.append(
                exp.Line2D(
                    [],
                    [],
                    linestyle="None",
                    marker="o",
                    markersize=5.5,
                    markerfacecolor=color,
                    markeredgecolor=color,
                    label=name,
                )
            )
        ticks, tick_labels = exp._date_ticks(labels)
        axis.set_xticks(ticks, tick_labels)
        top = max(float(bucket["share"] or 0) for row in bucket_rows for bucket in row)
        axis.set_ylim(0, max(10, math.ceil(top / 10) * 10 + 2))
        axis.yaxis.set_major_formatter(exp.FuncFormatter(lambda value, _pos: f"{value:.0f}%"))
        exp._style_axis(axis)
        legend = axis.legend(
            handles=handles,
            labels=bucket_names,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=min(6, len(bucket_names)),
            frameon=False,
            fontsize=8,
            handlelength=0.8,
            handletextpad=0.35,
            columnspacing=1.15,
            labelspacing=0.55,
        )
        for text in legend.get_texts():
            text.set_color("#2B323B")
            text.set_fontweight("normal")
        figure.subplots_adjust(left=0.14, right=0.98, top=0.98, bottom=0.25)
        return exp._save_figure(figure)


def _render_distribution_cards_table(
    exp,
    doc,
    distribution,
    depth,
    *,
    engine="yandex",
):
    buckets = exp._topvisor_buckets(
        distribution,
        20 if engine == "google" else depth,
    )
    if engine == "google":
        buckets = [bucket for bucket in buckets if bucket["label"] in {"1-3", "1-10", "11-20"}]
    if not buckets:
        return None
    columns = 1 if engine == "google" else 2
    row_count = math.ceil(len(buckets) / columns)
    table = doc.add_table(rows=row_count, cols=columns)
    table.autofit = False
    for row in table.rows:
        exp._prevent_row_split(row)
        for cell in row.cells:
            exp._set_cell_width(cell, 9.25 if columns == 2 else 9.6)
            exp._set_cell_margins(
                cell,
                top=55,
                bottom=55,
                left=70,
                right=70,
            )
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = ""
    for index, bucket in enumerate(buckets):
        column = index // row_count
        row_index = index % row_count
        outer = table.rows[row_index].cells[column]
        nested = outer.add_table(rows=1, cols=3)
        nested.autofit = False
        widths = (1.55, 1.45, 1.45)
        for grid_column, width in zip(
            nested._tbl.tblGrid.gridCol_lst,
            widths,
            strict=True,
        ):
            grid_column.w = exp.Cm(width)
        label_cell, share_cell, count_cell = nested.rows[0].cells
        for cell, width in zip(
            (label_cell, share_cell, count_cell),
            widths,
            strict=True,
        ):
            exp._set_cell_width(cell, width)
            exp._set_cell_margins(
                cell,
                top=35,
                bottom=35,
                left=35,
                right=35,
            )
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
        exp._shade_cell(label_cell, exp.TOPVISOR_COLORS[bucket["label"]])
        label_cell.text = str(bucket["label"])
        share_cell.text = exp._number(
            bucket.get("share"),
            "%",
            decimal_places=0,
        )
        count_cell.text = exp._number(bucket.get("count"), decimal_places=0)
        for paragraph in label_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=10, color="FFFFFF", bold=True)
        for paragraph in share_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=10, color="8491A5")
        for paragraph in count_cell.paragraphs:
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=10, color="3D4655")
        exp._set_table_borders(nested, "F0F2F5", size="0")
        for paragraph in outer.paragraphs:
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
    exp._set_table_borders(table, "FFFFFF", size="12")
    gap = doc.add_paragraph()
    gap.paragraph_format.space_after = exp.Pt(2)
    return table


def _visibility_comparison_phrase(exp, current, previous):
    if current is None or previous is None:
        return "нет данных для сравнения"
    current_number = Decimal(str(current))
    previous_number = Decimal(str(previous))
    if current_number == previous_number:
        return "не изменилась"
    # Visibility itself is already a percentage. Yandex/Topvisor comparisons in
    # this sentence are expressed in percentage points, not as a relative growth
    # rate from the previous percentage value.
    delta = current_number - previous_number
    direction = "увеличилась" if delta > 0 else "уменьшилась"
    rendered = exp._number(abs(delta), "%", decimal_places=0)
    return f"{direction} на {rendered}"


def _render_topvisor_comparison(exp, doc, segment, *, show_visibility=True):
    history = segment.get("three_month_series") or []
    if not history:
        return
    current = history[-1]
    previous = history[-2] if len(history) >= 2 else None
    depth = segment.get("ranking_depth") or 0
    current_buckets = {
        item["label"]: item
        for item in exp._topvisor_buckets(
            current.get("distribution") or {},
            depth,
        )
    }
    previous_buckets = (
        {
            item["label"]: item
            for item in exp._topvisor_buckets(
                previous.get("distribution") or {},
                depth,
            )
        }
        if previous
        else {}
    )
    final_label = "11-30" if depth >= 30 else "11-20"
    doc.add_paragraph("В сравнении с прошлым месяцем доля запросов в топ:")
    for label in ("1-3", "1-10", final_label):
        bucket = current_buckets.get(label) or {}
        now = bucket.get("share")
        before = (previous_buckets.get(label) or {}).get("share")
        rendered_label = {"1-3": "3", "1-10": "10"}.get(label, label)
        paragraph = doc.add_paragraph(
            f"Запросов в топ {rendered_label} — "
            f"{exp._number(now, '%', decimal_places=0)} "
            f"({exp._comparison_phrase(now, before)})."
        )
        paragraph.paragraph_format.space_after = exp.Pt(0)
    if show_visibility:
        now_visibility = current.get("visibility")
        previous_visibility = previous.get("visibility") if previous else None
        engine = exp.ENGINE_LABELS.get(segment.get("search_engine"), "Поиск")
        region = segment.get("region") or "регион не указан"
        comparison = _visibility_comparison_phrase(
            exp,
            now_visibility,
            previous_visibility,
        )
        paragraph = doc.add_paragraph(
            f"Общая видимость сайта в ПС {engine}.{region} — "
            f"{exp._number(now_visibility, '%', decimal_places=0)} "
            f"({comparison})."
        )
        paragraph.paragraph_format.space_after = exp.Pt(0)


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import forms as report_forms

    report_forms.validate_topvisor_manual_rows = validate_manual_rows

    original_init = report_forms.ReportCreateForm.__init__
    original_clean = report_forms.ReportCreateForm.clean

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        field = self.fields.get("month")
        if field is not None:
            field.widget = django_forms.HiddenInput()
            field.label = ""
            if not self.is_bound:
                self.initial["month"] = ""

    def patched_clean(self):
        cleaned = original_clean(self)
        selected_positions = any(cleaned.get(f"{engine}_dates") for engine in ("yandex", "google"))
        selected_sources = bool(
            (cleaned.get("include_metrika") and cleaned.get("metrika_snapshots"))
            or (cleaned.get("include_webmaster") and cleaned.get("webmaster_snapshots"))
        )
        if selected_positions or selected_sources:
            cleaned["month"] = None
        elif cleaned.get("month") is None:
            cleaned["month"] = getattr(self, "report_month", None)
        return cleaned

    report_forms.ReportCreateForm.__init__ = patched_init
    report_forms.ReportCreateForm.clean = patched_clean

    from . import exporting as exp

    exp.GENERATOR_VERSION = "mvp1.9-2026-09-05"
    exp.TOPVISOR_COLORS.update(
        {
            "1-3": "#3198DD",
            "1-10": "#21936C",
            "11-20": "#1ABC9C",
            "11-30": "#1ABC9C",
            "31-50": "#A2DF9F",
            "51-100": "#B0C7C7",
            "101+": "#FBC02D",
        }
    )
    exp._manual_topvisor_segment = _manual_topvisor_segment
    exp._visibility_chart = lambda points, title=None: _visibility_chart(exp, points, title)
    exp._distribution_chart = lambda history, depth: _distribution_chart(exp, history, depth)
    exp._render_distribution_cards_table = lambda doc, distribution, depth, *, engine="yandex": (
        _render_distribution_cards_table(
            exp,
            doc,
            distribution,
            depth,
            engine=engine,
        )
    )
    exp._render_topvisor_comparison = lambda doc, segment, *, show_visibility=True: (
        _render_topvisor_comparison(
            exp,
            doc,
            segment,
            show_visibility=show_visibility,
        )
    )

    # Views imports helpers by value, so keep its references aligned too.
    from . import views

    views._validated_manual_rows = validate_manual_rows
    views._manual_topvisor_segment = _manual_topvisor_segment
