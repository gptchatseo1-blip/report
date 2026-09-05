"""Final report/table polish requested on 2026-09-06."""

import math

_APPLIED = False


def _manual_buckets_with_yandex_tail(base_buckets, distribution, depth):
    manual = distribution.get("manual_buckets") or {}
    if not manual:
        return base_buckets(distribution, depth)
    final_label = "11-30" if depth >= 30 else "11-20"
    labels = ["1-3", "1-10", final_label]
    if depth >= 50 and "31-50" in manual:
        labels.append("31-50")
    if depth >= 100:
        for label in ("51-100", "101+"):
            if label in manual:
                labels.append(label)
    return [
        {
            "label": label,
            "count": (manual.get(label) or {}).get("count", 0),
            "share": (manual.get(label) or {}).get("share"),
        }
        for label in labels
    ]


def _merge_yandex_tail_buckets(base_buckets, source_segment, rendered):
    depth = rendered.get("ranking_depth") or 0
    if depth < 50:
        return rendered
    source_by_month = {
        str(point.get("month"))[:7]: point
        for point in source_segment.get("three_month_series") or []
        if point.get("month")
    }

    def merge_point(point):
        month_key = str(point.get("month") or "")[:7]
        source = source_by_month.get(month_key)
        if not source or not point.get("manual_override"):
            return point
        distribution = dict(point.get("distribution") or {})
        manual = dict(distribution.get("manual_buckets") or {})
        automatic = {
            bucket["label"]: bucket
            for bucket in base_buckets(source.get("distribution") or {}, depth)
        }
        for label in ("31-50", "51-100", "101+"):
            if label in automatic:
                manual[label] = {
                    "count": automatic[label].get("count", 0),
                    "share": automatic[label].get("share"),
                }
        distribution["manual_buckets"] = manual
        return {**point, "distribution": distribution}

    history = [merge_point(point) for point in rendered.get("three_month_series") or []]
    monthly = [merge_point(point) for point in rendered.get("monthly_table_series") or []]
    result = {**rendered, "three_month_series": history}
    if "monthly_table_series" in rendered:
        result["monthly_table_series"] = monthly
    if history:
        result["distribution"] = history[-1].get("distribution") or result.get("distribution") or {}
    return result


def _calendar_chart_segment(base_manual_segment, base_buckets, payload, source_segment):
    """Graphs use only calendar-selected dates while manual values remain month-based."""
    rendered = base_manual_segment(payload, source_segment)
    rendered = _merge_yandex_tail_buckets(base_buckets, source_segment, rendered)
    engine = str(rendered.get("search_engine") or "").casefold()
    selection = payload.get("source_selection", {}).get("topvisor", {}).get(engine, {})
    selected_dates = [str(value)[:10] for value in selection.get("selected_dates") or []]
    if not selected_dates:
        return rendered

    source_by_day = {
        str(point.get("month"))[:10]: point
        for point in source_segment.get("chart_series") or []
        if point.get("month")
    }
    manual_by_month = {
        str(point.get("month"))[:7]: point
        for point in rendered.get("three_month_series") or []
        if point.get("month")
    }
    chart_series = []
    for selected_date in selected_dates:
        source = source_by_day.get(selected_date)
        manual = manual_by_month.get(selected_date[:7])
        point = dict(source or manual or {})
        if not point:
            continue
        point["month"] = selected_date
        if manual and manual.get("manual_override"):
            if manual.get("visibility") is not None:
                point["visibility"] = manual.get("visibility")
            if manual.get("distribution"):
                point["distribution"] = manual.get("distribution")
        chart_series.append(point)
    return {**rendered, "chart_series": chart_series}


def _compact_distribution_cards_table(exp, doc, distribution, depth, *, engine="yandex"):
    buckets = exp._topvisor_buckets(distribution, 20 if engine == "google" else depth)
    if engine == "google":
        buckets = [bucket for bucket in buckets if bucket["label"] in {"1-3", "1-10", "11-20"}]
    if not buckets:
        return None

    columns = 1 if engine == "google" else 2
    row_count = math.ceil(len(buckets) / columns)
    table = doc.add_table(rows=row_count, cols=columns)
    table.autofit = False
    outer_width = 4.15 if columns == 2 else 4.35

    for row in table.rows:
        exp._prevent_row_split(row)
        for cell in row.cells:
            exp._set_cell_width(cell, outer_width)
            exp._set_cell_margins(cell, top=5, bottom=5, left=18, right=18)
            exp._shade_cell(cell, "F0F2F5")
            cell.vertical_alignment = exp.WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = ""

    for index, bucket in enumerate(buckets):
        column = index // row_count
        row_index = index % row_count
        outer = table.rows[row_index].cells[column]
        nested = outer.add_table(rows=1, cols=3)
        nested.autofit = False
        widths = (1.35, 0.95, 1.45)
        for grid_column, width in zip(nested._tbl.tblGrid.gridCol_lst, widths, strict=True):
            grid_column.w = exp.Cm(width)
        label_cell, share_cell, count_cell = nested.rows[0].cells
        for cell, width in zip((label_cell, share_cell, count_cell), widths, strict=True):
            exp._set_cell_width(cell, width)
            exp._set_cell_margins(cell, top=4, bottom=4, left=10, right=10)
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
            paragraph.alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)
            for run in paragraph.runs:
                exp._style_run(run, size=11, color="3D4655")
        exp._set_table_borders(nested, "F0F2F5", size="0")
        for paragraph in outer.paragraphs:
            paragraph.paragraph_format.space_before = exp.Pt(0)
            paragraph.paragraph_format.space_after = exp.Pt(0)

    occupied = {(index % row_count, index // row_count) for index in range(len(buckets))}
    for row_index, row in enumerate(table.rows):
        for column, cell in enumerate(row.cells):
            if (row_index, column) in occupied:
                continue
            exp._shade_cell(cell, "FFFFFF")
            exp._set_cell_margins(cell, top=0, bottom=0, left=0, right=0)

    exp._set_table_borders(table, "FFFFFF", size="4")
    exp._keep_small_table_together(table)
    gap = doc.add_paragraph()
    gap.paragraph_format.space_before = exp.Pt(0)
    gap.paragraph_format.space_after = exp.Pt(1)
    gap.add_run("\u200b").font.size = exp.Pt(1)
    return table


def _distribution_chart_with_visibility(exp, history, depth):
    useful_rows = [row for row in history if (row.get("distribution") or {}).get("total")]
    if not useful_rows:
        return None
    bucket_rows = [
        exp._topvisor_buckets(row.get("distribution") or {}, depth) for row in useful_rows
    ]
    labels = [exp._date_label(row.get("month")) for row in useful_rows]
    x_values = list(range(len(labels)))
    with exp.plt.rc_context({"font.family": exp.CHART_FONT, "font.size": 9}):
        figure, axis = exp.plt.subplots(figsize=(7.2, 3.35), dpi=150, facecolor="white")
        bucket_names = [bucket["label"] for bucket in bucket_rows[0]]
        rows_by_name = [{bucket["label"]: bucket for bucket in buckets} for buckets in bucket_rows]
        handles = []
        if depth >= 50:
            visibility = [row.get("visibility") for row in useful_rows]
            if any(value is not None for value in visibility):
                values = [float(value or 0) for value in visibility]
                handle = exp._plot_smooth_line(
                    axis,
                    x_values,
                    values,
                    color=exp.TOPVISOR_COLORS["visibility"],
                    linewidth=1.9,
                    label="Видимость",
                )
                axis.scatter(
                    x_values,
                    values,
                    color=exp.TOPVISOR_COLORS["visibility"],
                    s=13,
                    zorder=3,
                )
                handles.append(handle)
        for name in bucket_names:
            values = [float(row.get(name, {}).get("share") or 0) for row in rows_by_name]
            color = exp.TOPVISOR_COLORS[name]
            handle = exp._plot_smooth_line(
                axis,
                x_values,
                values,
                color=color,
                linewidth=1.7,
                label=name,
            )
            axis.scatter(x_values, values, color=color, s=12, zorder=3)
            axis.fill_between(x_values, values, color=color, alpha=0.055)
            handles.append(handle)
        ticks, tick_labels = exp._date_ticks(labels)
        axis.set_xticks(ticks, tick_labels)
        values_for_top = [
            float(bucket.get("share") or 0)
            for row in bucket_rows
            for bucket in row
        ]
        if depth >= 50:
            values_for_top.extend(
                float(row.get("visibility") or 0)
                for row in useful_rows
                if row.get("visibility") is not None
            )
        top = max(values_for_top or [0])
        axis.set_ylim(0, max(10, math.ceil(top / 10) * 10 + 2))
        axis.yaxis.set_major_formatter(exp.FuncFormatter(lambda value, _pos: f"{value:.0f}%"))
        exp._style_axis(axis)
        axis.legend(
            handles=handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=min(7, len(handles)),
            frameon=False,
            fontsize=7.5,
        )
        figure.subplots_adjust(left=0.14, right=0.98, top=0.98, bottom=0.25)
        return exp._save_figure(figure)


def _render_monthly_table_with_visibility(exp, doc, segment):
    series = segment.get("monthly_table_series")
    source = {**segment, "three_month_series": series} if series is not None else segment
    rows = exp._monthly_topvisor_rows(source)
    if not rows:
        doc.add_paragraph("Месячные итоги отсутствуют.", style="Data Missing")
        return None
    final_label = "11-30" if (segment.get("ranking_depth") or 0) >= 30 else "11-20"
    table = exp._table(
        doc,
        ("Месяц", "Видимость", "в топ 3", "в топ 10", f"в топ {final_label}"),
        rows,
        [3.5, 2.6, 4.0, 4.0, 4.0],
        header_fill="EEF1F2",
    )
    for row in table.rows:
        for column in range(1, len(row.cells)):
            row.cells[column].paragraphs[0].alignment = exp.WD_ALIGN_PARAGRAPH.CENTER
    return table


def apply():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    from . import exporting as exp
    from . import views

    current_manual_segment = exp._manual_topvisor_segment
    current_buckets = exp._topvisor_buckets

    def manual_segment(payload, segment):
        return _calendar_chart_segment(current_manual_segment, current_buckets, payload, segment)

    exp.GENERATOR_VERSION = "mvp1.11-2026-09-06"
    exp._topvisor_buckets = lambda distribution, depth: _manual_buckets_with_yandex_tail(
        current_buckets, distribution, depth
    )
    exp._manual_topvisor_segment = manual_segment
    views._manual_topvisor_segment = manual_segment
    exp._distribution_chart = lambda history, depth: _distribution_chart_with_visibility(
        exp, history, depth
    )
    exp._render_distribution_cards_table = (
        lambda doc, distribution, depth, *, engine="yandex": _compact_distribution_cards_table(
            exp, doc, distribution, depth, engine=engine
        )
    )
    exp._render_monthly_topvisor_table = (
        lambda doc, segment, *, show_visibility=True: _render_monthly_table_with_visibility(
            exp, doc, segment
        )
    )
