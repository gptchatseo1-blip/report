"""Offline renderers whose business data comes only from a frozen report snapshot."""

import hashlib
import io
import re
import subprocess
import tempfile
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import matplotlib
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

from .models import GeneratedArtifact, NarrativeBlock, ReportDatasetSnapshot, ValidationIssue
from .narratives import SECTION_ORDER, TOP_SECTION_RANGES, section_enabled
from .validation import get_publication_readiness

GENERATOR_VERSION = "mvp1.1"
MIMES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
TITLES = {
    "visibility": "Видимость",
    "position_distribution": "Распределение позиций",
    "top_5": "Запросы в TOP-5",
    "top_10": "Запросы в TOP-10",
    "top_20": "Запросы в TOP-20",
    "top_11_30": "Запросы в TOP-11–30",
    "top_30": "Запросы в TOP-30",
    "top_11_20": "TOP-11–20",
    "position_dynamics": "Динамика позиций по месяцам",
    "traffic": "Трафик",
    "traffic_sources": "Источники трафика",
    "clicks_impressions": "Клики и показы",
    "ctr": "CTR",
    "indexing": "Индексация",
    "iks": "ИКС",
    "geography": "География посетителей",
    "completed_work": "Выполненные работы",
}
MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
ENGINE_LABELS = {"google": "Google", "yandex": "Яндекс"}
METRIC_LABELS = {
    "visits": "Визиты",
    "users": "Посетители",
    "new_users": "Новые посетители",
    "bounce_rate": "Показатель отказов",
    "page_depth": "Глубина просмотра",
    "avg_visit_duration_seconds": "Средняя длительность визита",
    "search_clicks": "Клики",
    "search_impressions": "Показы",
    "search_ctr": "CTR",
    "indexed_pages": "Индексируемые страницы",
    "iks": "ИКС",
    "quality_index": "ИКС",
    "geography_moscow_visits": "Москва",
    "geography_saint_petersburg_visits": "Санкт-Петербург",
    "geography_undefined_visits": "Не определено",
    "geography_area_undefined_visits": "Область не определена",
}
CHART_PALETTES = {
    "topvisor": ("#27B98B", "#2D8FC4", "#64D1AF", "#A3E3D1", "#CFEBE3", "#AAB7B3"),
    "webmaster": ("#F2C94C", "#33A852", "#EF8354", "#5AA9E6", "#8D6A9F"),
    "metrika": ("#7B3FE4", "#D13ACB", "#27A6D5", "#6BCB77", "#FF8A5B", "#B58CE8"),
}


class ExportBlocked(Exception):
    pass


def _clean(value):
    rendered = "" if value is None else str(value)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", rendered)


def _excel_safe(value):
    """Prevent spreadsheet programs from interpreting untrusted strings as formulas."""
    value = _clean(value)
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def _number(value, suffix="", *, decimal_places=None):
    if value is None:
        return "Данные недоступны"
    try:
        number = Decimal(str(value))
        if decimal_places is None:
            rendered = format(number.normalize(), "f")
        else:
            quantum = Decimal("1").scaleb(-decimal_places)
            rendered = format(
                number.quantize(quantum, rounding=ROUND_HALF_UP), f".{decimal_places}f"
            )
        rendered = rendered.replace(".", ",")
    except (InvalidOperation, ValueError):
        rendered = _clean(value)
    return rendered + suffix


def _month(value):
    parsed = date.fromisoformat(str(value)[:10])
    return f"{MONTHS[parsed.month - 1]} {parsed.year}"


def _month_short(value):
    parsed = date.fromisoformat(str(value)[:10])
    return f"{parsed.month:02d}.{parsed.year}"


def _set_cell_width(cell, width):
    cell.width = Cm(width)
    tc_width = cell._tc.get_or_add_tcPr().get_or_add_tcW()
    tc_width.set(qn("w:w"), str(int(width * 567)))
    tc_width.set(qn("w:type"), "dxa")


def _repeat_header(row):
    props = row._tr.get_or_add_trPr()
    element = OxmlElement("w:tblHeader")
    element.set(qn("w:val"), "true")
    props.append(element)


def _prevent_row_split(row):
    props = row._tr.get_or_add_trPr()
    props.append(OxmlElement("w:cantSplit"))


def _keep_small_table_together(table):
    """Keep compact tables on one page instead of orphaning their final row."""
    if len(table.rows) > 8:
        return
    for row in table.rows[:-1]:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True


def _shade_cell(cell, color):
    if not color:
        return
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), color.removeprefix("#"))


def _table(doc, headers, rows, widths=None, *, header_fill=None, cell_fills=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Report Table"
    table.autofit = False
    widths = widths or [16 / len(headers)] * len(headers)
    for cell, value, width in zip(table.rows[0].cells, headers, widths, strict=True):
        cell.text = _clean(value)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _set_cell_width(cell, width)
        _shade_cell(cell, header_fill)
    _repeat_header(table.rows[0])
    _prevent_row_split(table.rows[0])
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        _prevent_row_split(table.rows[-1])
        for column_index, (cell, value, width) in enumerate(
            zip(cells, values, widths, strict=True)
        ):
            cell.text = _clean(value if value is not None and value != "" else "—")
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            _set_cell_width(cell, width)
            if cell_fills and row_index < len(cell_fills):
                fills = cell_fills[row_index]
                if column_index < len(fills):
                    _shade_cell(cell, fills[column_index])
    _keep_small_table_together(table)
    return table


def _chart(series, *, title, ylabel, kind="line", style="topvisor"):
    """Return a stable PNG, or None when no series has at least one known value."""
    useful = [(label, points) for label, points in series if any(v is not None for _, v in points)]
    if not useful:
        return None
    with plt.rc_context({"font.family": "Carlito", "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.4), dpi=120, facecolor="white")
        palette = CHART_PALETTES.get(style, CHART_PALETTES["topvisor"])
        for index, (label, points) in enumerate(useful):
            labels = [_month_short(month) for month, _ in points]
            values = [float(value) if value is not None else float("nan") for _, value in points]
            color = palette[index % len(palette)]
            if kind == "bar":
                offsets = [
                    (i - (len(useful) - 1) / 2) * 0.75 / len(useful) for i in range(len(useful))
                ]
                x = [position + offsets[index] for position in range(len(labels))]
                bars = axis.bar(x, values, width=0.75 / len(useful), color=color, label=label)
                if style == "metrika" and index % 2:
                    for bar in bars:
                        bar.set_hatch("////")
                        bar.set_edgecolor(color)
                        bar.set_facecolor("white")
                axis.set_xticks(range(len(labels)), labels)
            else:
                axis.plot(
                    labels,
                    values,
                    marker="o" if style == "metrika" else None,
                    markersize=4,
                    linewidth=2.2 if style == "topvisor" else 2,
                    color=color,
                    label=label,
                )
                if style == "webmaster" and len(useful) == 1:
                    axis.fill_between(labels, values, alpha=0.14, color=color)
        axis.set_title(title, loc="left", fontweight="bold", color="#24313D")
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", color="#E9EDF1", linewidth=0.8)
        axis.set_axisbelow(True)
        for spine in axis.spines.values():
            spine.set_visible(False)
        if len(useful) > 6:
            axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
            figure.tight_layout(rect=(0, 0, 0.78, 1))
        else:
            if len(useful) > 1:
                axis.legend(loc="best")
            figure.tight_layout()
        output = io.BytesIO()
        figure.savefig(output, format="png", dpi=120, metadata={"Software": GENERATOR_VERSION})
        plt.close(figure)
        output.seek(0)
        return output


def _add_chart(doc, chart, narrative):
    if chart is None:
        doc.add_paragraph("Данные недоступны для построения графика.", style="Data Missing")
        return False
    doc.add_picture(chart, width=Cm(16))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(_clean(narrative), style="Chart Caption")
    return True


def _visibility_chart(points, title):
    useful = [(month, value) for month, value in points if value is not None]
    if not useful:
        return None
    with plt.rc_context({"font.family": "Carlito", "font.size": 9}):
        figure = plt.figure(figsize=(7.2, 3.25), dpi=120, facecolor="white")
        grid = figure.add_gridspec(1, 2, width_ratios=(3.5, 1.2), wspace=0.22)
        axis = figure.add_subplot(grid[0, 0])
        labels = [_month_short(month) for month, _value in useful]
        values = [float(value) for _month, value in useful]
        axis.plot(labels, values, color="#27B98B", linewidth=2.5)
        axis.fill_between(labels, values, color="#27B98B", alpha=0.08)
        axis.set_title(title, loc="left", fontweight="bold", color="#24313D")
        axis.set_ylabel("%")
        axis.grid(axis="y", color="#E9EDF1", linewidth=0.8)
        axis.set_axisbelow(True)
        for spine in axis.spines.values():
            spine.set_visible(False)
        donut = figure.add_subplot(grid[0, 1])
        current = max(0, min(100, values[-1]))
        donut.pie(
            [current, 100 - current],
            startangle=90,
            counterclock=False,
            colors=("#27B98B", "#EDF3F1"),
            wedgeprops={"width": 0.22, "edgecolor": "white"},
        )
        donut.text(
            0,
            0.05,
            f"{current:.1f}%".replace(".", ","),
            ha="center",
            va="center",
            fontsize=14,
            fontweight="bold",
            color="#174D3C",
        )
        donut.text(0, -0.22, "видимость", ha="center", va="center", fontsize=8, color="#667085")
        donut.set_aspect("equal")
        figure.subplots_adjust(left=0.08, right=0.98, top=0.88, bottom=0.16)
        output = io.BytesIO()
        figure.savefig(output, format="png", dpi=120, metadata={"Software": GENERATOR_VERSION})
        plt.close(figure)
        output.seek(0)
        return output


def _segment_title(segment):
    engine = ENGINE_LABELS.get(
        segment.get("search_engine"), segment.get("search_engine") or "Поиск"
    )
    return f"{engine} · {segment.get('region') or 'регион не указан'}"


def _metric_source(payload, source_name):
    return payload.get("calculated", {}).get("sources", {}).get("sources", {}).get(source_name, {})


def _current_position_source(payload, segment):
    configuration_id = segment.get("configuration_id")
    candidates = [
        s
        for s in payload.get("ranking_sources", [])
        if s.get("search_engine") == segment.get("search_engine")
        and s.get("region") == segment.get("region")
        and (not configuration_id or s.get("configuration_id") == configuration_id)
    ]
    return max(
        candidates, key=lambda item: (item.get("date") or "", item.get("id") or ""), default=None
    )


def _position_rows(source, start=None, end=None, *, show_urls=True):
    if not source:
        return []
    rows = []
    depth = source.get("ranking_depth") or 0
    for item in source.get("positions", []):
        position = item.get("position")
        if position is not None and position > depth:
            position = None
        if start is not None and (position is None or position < start or position > end):
            continue
        values = [
            item.get("query"),
            item.get("frequency"),
            position,
            item.get("group"),
            item.get("target_url"),
        ]
        rows.append(tuple(values if show_urls else values[:-1]))
    return rows


def _start_landscape(doc):
    section = doc.add_section()
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Cm(29.7), Cm(21)


def _return_to_portrait(doc):
    portrait = doc.add_section()
    portrait.orientation = WD_ORIENT.PORTRAIT
    portrait.page_width, portrait.page_height = Cm(21), Cm(29.7)


def _render_visibility(doc, payload, segments, narrative):
    if not segments:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
    else:
        doc.add_paragraph(_clean(narrative))
    for segment in segments:
        doc.add_heading(_segment_title(segment), level=2)
        points = [
            (row.get("month"), row.get("visibility"))
            for row in segment.get("three_month_series", [])
        ]
        _add_chart(
            doc,
            _visibility_chart(points, f"Видимость · {_segment_title(segment)}"),
            f"Динамика видимости за выбранные месяцы · {_segment_title(segment)}.",
        )
        current = points[-1][1] if points else None
        doc.add_paragraph(f"Текущая видимость: {_number(current, '%')}", style="KPI")
        _table(
            doc,
            ("Период", "Видимость, %"),
            [(_month(month), _number(value)) for month, value in points],
            [8, 8],
        )


def _render_distribution(doc, payload, segments, narrative):
    if not segments:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
    else:
        doc.add_paragraph(_clean(narrative))
    for segment in segments:
        doc.add_heading(_segment_title(segment), level=2)
        distribution = segment.get("distribution") or {}
        ranges = distribution.get("ranges") or {}
        total = distribution.get("total")
        current = [(name, count * 100 / total if total else None) for name, count in ranges.items()]
        chart = _chart(
            [
                (name, [(payload["periods"]["report"]["start"], value)])
                for name, value in ranges.items()
            ],
            title=f"Распределение запросов · {_segment_title(segment)}",
            ylabel="Количество",
            kind="bar",
            style="topvisor",
        )
        _add_chart(
            doc,
            chart,
            f"Распределение запросов по позициям · {_segment_title(segment)}.",
        )
        fill_palette = ("268BD2", "28B98B", "62CFB0", "A7E5D4", "DCEEE9", "EEF4F2")
        _table(
            doc,
            ("Диапазон", "Количество", "Доля, %"),
            [(name, ranges[name], _number(share, decimal_places=1)) for name, share in current],
            [6, 5, 5],
            header_fill="E7F6EF",
            cell_fills=[(fill, fill, fill) for fill in fill_palette[: len(current)]],
        )
    options = payload.get("display_options", {})
    if options.get("include_topvisor_report_link") and options.get("topvisor_report_url"):
        doc.add_paragraph(
            "Подробный отчёт Topvisor: " + _clean(options["topvisor_report_url"]),
            style="Depth Note",
        )


def _render_top(doc, payload, segments, narrative, start, end, code):
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    any_segment = False
    for segment in segments:
        source = _current_position_source(payload, segment)
        rows = _position_rows(source, start, end, show_urls=show_urls)
        if (
            code == "top_11_20"
            and not rows
            and payload.get("project", {}).get("top_11_20_mode") != "enabled"
        ):
            continue
        any_segment = True
        range_label = f"TOP-{end}" if start == 1 else f"TOP-{start}–{end}"
        doc.add_heading(f"{_segment_title(segment)} · {range_label}", level=2)
        if code == "top_10":
            history = segment.get("three_month_series") or []
            top_series = [
                (
                    "TOP-10",
                    [
                        (row.get("month"), (row.get("distribution") or {}).get("top_10"))
                        for row in history
                    ],
                )
            ]
            if history and all((row.get("ranking_depth") or 0) >= 30 for row in history):
                top_series.append(
                    (
                        "TOP-30",
                        [
                            (row.get("month"), (row.get("distribution") or {}).get("top_30"))
                            for row in history
                        ],
                    )
                )
            _add_chart(
                doc,
                _chart(
                    top_series,
                    title=f"Динамика TOP · {_segment_title(segment)}",
                    ylabel="Количество запросов",
                    style="topvisor",
                ),
                f"Динамика количества запросов · {_segment_title(segment)}.",
            )
            distribution = segment.get("distribution") or {}
            kpi = f"TOP-10: {_number(distribution.get('top_10'))}"
            if (segment.get("ranking_depth") or 0) >= 30:
                kpi += f" · TOP-30: {_number(distribution.get('top_30'))}"
            doc.add_paragraph(kpi, style="KPI")
        if rows:
            headers = (
                ("Запрос", "Частотность", "Позиция", "Группа", "Релевантный URL")
                if show_urls
                else ("Запрос", "Частотность", "Позиция", "Группа")
            )
            position_colors = []
            for row in rows:
                position = row[2]
                color = (
                    "27C493"
                    if position <= 3
                    else "62D8B5"
                    if position <= 5
                    else "91E2C9"
                    if position <= 10
                    else "C2EDDF"
                    if position <= 20
                    else "E6F6F1"
                )
                fills = [None] * len(headers)
                fills[2] = color
                position_colors.append(fills)
            _table(
                doc,
                headers,
                rows,
                [5.0, 2.3, 2.0, 3.0, 4.0] if show_urls else [7.0, 2.5, 2.5, 4.0],
                header_fill="E7F6EF",
                cell_fills=position_colors,
            )
        else:
            doc.add_paragraph("Запросы в диапазоне отсутствуют.", style="Data Missing")
    if any_segment:
        doc.add_paragraph(_clean(narrative))
    return any_segment


def _render_position_dynamics(doc, payload, segments, narrative):
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    if not segments:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
    else:
        doc.add_paragraph(_clean(narrative))
    for segment in segments:
        doc.add_heading(_segment_title(segment), level=2)
        series = segment.get("three_month_series") or []
        labels = []
        confirmed_ranges = (
            set.intersection(
                *[set((row.get("distribution") or {}).get("ranges", {})) for row in series]
            )
            if series
            else set()
        )
        range_series = []
        for name in ("1-3", "4-10", "11-20", "21-30", "31-50", "51-100"):
            if name in confirmed_ranges:
                labels.append(name)
                range_series.append(
                    (
                        name,
                        [
                            (
                                row.get("month"),
                                (row.get("distribution") or {}).get("ranges", {}).get(name),
                            )
                            for row in series
                        ],
                    )
                )
        _add_chart(
            doc,
            _chart(
                range_series,
                title=f"Трёхмесячная динамика позиций · {_segment_title(segment)}",
                ylabel="Количество",
                kind="line",
                style="topvisor",
            ),
            f"Динамика распределения запросов · {_segment_title(segment)}.",
        )
        history_rows = []
        for row in series:
            dist = row.get("distribution") or {}
            ranges = dist.get("ranges") or {}
            top_10 = dist.get("top_10")
            top_30 = dist.get("top_30") if (row.get("ranking_depth") or 0) >= 30 else None
            history_rows.append(
                (
                    _month(row.get("month")),
                    row.get("ranking_depth"),
                    "; ".join(f"{key}: {ranges[key]}" for key in labels),
                    top_10,
                    top_30,
                )
            )
        headers = ["Период", "Глубина", "Подтверждённые диапазоны", "TOP-10"]
        widths = [3.0, 2.3, 7.7, 3.0]
        if (segment.get("ranking_depth") or 0) >= 30:
            headers.append("TOP-30")
            widths = [2.7, 2.2, 6.1, 2.5, 2.5]
        else:
            history_rows = [row[:-1] for row in history_rows]
        _table(doc, headers, history_rows, widths)
        source = _current_position_source(payload, segment)
        all_rows = _position_rows(source, show_urls=show_urls)
        _start_landscape(doc)
        doc.add_heading("Все запросы отчётного периода", level=3)
        if all_rows:
            headers = (
                ("Запрос", "Частотность", "Позиция", "Группа", "Релевантный URL")
                if show_urls
                else ("Запрос", "Частотность", "Позиция", "Группа")
            )
            _table(
                doc,
                headers,
                all_rows,
                [7.0, 2.7, 2.2, 5.0, 9.0] if show_urls else [11.0, 3.5, 3.0, 8.4],
            )
        else:
            doc.add_paragraph("Данные недоступны.", style="Data Missing")
        _return_to_portrait(doc)
    google = [s for s in segments if s.get("search_engine") == "google" and s.get("ranking_depth")]
    if google:
        configurations = "; ".join(
            f"{s.get('region') or 'регион не указан'} — TOP-{s['ranking_depth']}" for s in google
        )
        doc.add_paragraph(
            f"Глубина проверки Google: {configurations}. Для запросов вне подтверждённой "
            "глубины точная позиция не определена.",
            style="Depth Note",
        )


def _metric_series(payload, source, codes):
    facts = _metric_source(payload, source)
    result = []
    for code in codes:
        points = [
            (row.get("month"), row.get("value"))
            for row in facts.get("three_month_series", {}).get(code, [])
        ]
        result.append((METRIC_LABELS.get(code, code), points))
    return result


def _metric_has_data(payload, source, code):
    facts = _metric_source(payload, source)
    change = (facts.get("normalized_changes") or {}).get(code) or {}
    if any(change.get(field) is not None for field in ("current", "previous")):
        return True
    return any(
        point.get("value") is not None
        for point in (facts.get("three_month_series") or {}).get(code, [])
    )


def _change_rows(payload, source, codes):
    changes = _metric_source(payload, source).get("normalized_changes", {})
    rows = []
    for code in codes:
        change = changes.get(code)
        if not change:
            rows.append(
                (
                    METRIC_LABELS.get(code, code),
                    "Данные недоступны",
                    "Данные недоступны",
                    "Данные недоступны",
                    "Данные недоступны",
                )
            )
            continue
        unit = "%" if change.get("percentage_points") is not None else ""
        delta_label = "процентных пунктов" if unit else ""
        rows.append(
            (
                METRIC_LABELS.get(code, code),
                _number(change.get("previous"), unit),
                _number(change.get("current"), unit),
                _number(change.get("absolute"), f" {delta_label}"),
                _number(change.get("relative_percent"), "%"),
            )
        )
    return rows


def _render_metrics(doc, payload, code, narrative):
    configs = {
        "traffic": (
            "yandex_metrika",
            (
                "visits",
                "users",
                "new_users",
                "bounce_rate",
                "page_depth",
                "avg_visit_duration_seconds",
            ),
            "Основные показатели и цели Метрики",
            "Значение",
        ),
        "clicks_impressions": (
            "yandex_webmaster",
            ("search_impressions", "search_clicks"),
            "Показы и клики",
            "Количество",
        ),
        "ctr": ("yandex_webmaster", ("search_ctr",), "CTR", "%"),
        "indexing": ("yandex_webmaster", ("indexed_pages",), "Индексируемые страницы", "Страницы"),
        "iks": ("yandex_webmaster", ("iks", "quality_index"), "ИКС", "Значение"),
        "geography": (
            "yandex_metrika",
            (
                "geography_moscow_visits",
                "geography_saint_petersburg_visits",
                "geography_undefined_visits",
                "geography_area_undefined_visits",
            ),
            "География посетителей",
            "Визиты",
        ),
    }
    source, codes, title, ylabel = configs[code]
    if code == "traffic":
        available = _metric_source(payload, source).get("three_month_series", {})
        codes = (*codes, *(key for key in sorted(available) if key.startswith("goal_")))
    if code == "iks":
        codes = tuple(metric for metric in codes if _metric_has_data(payload, source, metric))[:1]
    if code == "geography":
        options = payload.get("display_options", {})
        flags = {
            "geography_moscow_visits": "geography_moscow",
            "geography_saint_petersburg_visits": "geography_saint_petersburg",
            "geography_undefined_visits": "geography_undefined",
            "geography_area_undefined_visits": "geography_area_undefined",
        }
        codes = tuple(metric for metric in codes if options.get(flags[metric], True))
    chart_codes = codes
    chart_kind = "line"
    if code == "traffic":
        chart_codes = tuple(
            metric
            for metric in codes
            if metric in {"visits", "users", "new_users"} or metric.endswith("_reaches")
        )
        chart_kind = "bar"
    series = _metric_series(payload, source, chart_codes)
    style = "metrika" if source == "yandex_metrika" else "webmaster"
    _add_chart(
        doc,
        _chart(series, title=title, ylabel=ylabel, style=style, kind=chart_kind),
        narrative,
    )
    _table(
        doc,
        (
            "Показатель",
            "Предыдущий период",
            "Текущий период",
            "Абсолютное изменение",
            "Относительное изменение",
        ),
        _change_rows(payload, source, codes),
        [3.7, 3.1, 3.1, 3.1, 3.0],
        header_fill="EFE7FF" if style == "metrika" else "FFF4C9",
    )


def _render_traffic_sources(doc, payload, narrative):
    facts = _metric_source(payload, "yandex_metrika").get("traffic_source_dynamics", {})
    series = [
        (
            name,
            [(point.get("month"), point.get("value")) for point in fact.get("series", [])],
        )
        for name, fact in facts.items()
    ]
    _add_chart(
        doc,
        _chart(
            series,
            title="Структура источников трафика",
            ylabel="Визиты",
            style="metrika",
        ),
        narrative,
    )
    rows = []
    for name, fact in facts.items():
        change = fact.get("change") or {}
        rows.append(
            (
                name,
                _number(change.get("current")),
                _number(fact.get("share_percent"), "%", decimal_places=1),
                _number(change.get("absolute")),
                _number(change.get("relative_percent"), "%"),
            )
        )
    if rows:
        _table(
            doc,
            ("Источник", "Количество", "Доля", "Абсолютное изменение", "Относительное изменение"),
            rows,
            [4.2, 2.8, 2.5, 3.3, 3.2],
            header_fill="EFE7FF",
        )
    else:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")


def _render_work(doc, payload, narrative):
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    rows = [
        (
            w.get("date"),
            w.get("category"),
            w.get("title"),
            w.get("status"),
            w.get("page_or_material_name") or w.get("url"),
            w.get("character_count"),
            w.get("responsible"),
            w.get("comment"),
            w.get("result_url"),
        )
        for w in payload.get("completed_work", [])
    ]
    if rows:
        if not show_urls:
            rows = [row[:4] + row[5:8] for row in rows]
        doc.add_heading("Таблица выполненных работ", level=3)
        headers = [
            "Дата",
            "Категория",
            "Название",
            "Статус",
            "Объём",
            "Ответственный",
            "Комментарий",
        ]
        widths = [2.2, 3.0, 4.5, 2.5, 2.0, 3.2, 4.2]
        if show_urls:
            headers.insert(4, "Страница или материал")
            headers.append("Результат")
            widths = [2.2, 3.0, 4.5, 2.5, 4.2, 2.0, 3.2, 4.2, 4.2]
        _table(doc, headers, rows, widths)
        doc.add_paragraph(_clean(narrative))
    else:
        doc.add_paragraph(
            _clean(narrative) or "Выполненные работы отсутствуют.", style="Data Missing"
        )


def _configure_document(doc, domain, period):
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2)
    section.right_margin = Cm(1.7)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[name]
        style.font.name = "Carlito"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Carlito")
        style.font.color.rgb = RGBColor.from_string("263746")
    doc.styles["Normal"].font.size = Pt(10.5)
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        doc.styles[name].paragraph_format.keep_with_next = True
    for style_name, size, italic in (
        ("Chart Caption", 9, True),
        ("Data Missing", 10, True),
        ("KPI", 15, False),
        ("Depth Note", 9, True),
    ):
        style = doc.styles.add_style(style_name, 1)
        style.font.name = "Carlito"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Carlito")
        style.font.size = Pt(size)
        style.font.italic = italic
    table_style = doc.styles.add_style("Report Table", 3)
    table_style.base_style = doc.styles["Table Grid"]
    table_style.font.name = "Carlito"
    table_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Carlito")
    table_style.font.size = Pt(8.5)
    footer = section.footer.paragraphs[0]
    footer.text = f"{_clean(domain)} · {_month(period)} · стр. "
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)


def _docx(snapshot, narratives, issues, draft):
    payload = snapshot.payload
    project = payload.get("project", {})
    period = payload.get("periods", {}).get("report", {}).get("start")
    doc = Document()
    _configure_document(doc, project.get("domain"), period)
    if draft:
        paragraph = doc.add_paragraph("ЧЕРНОВИК")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.runs[0].bold = True
        paragraph.runs[0].font.size = Pt(26)
        paragraph.runs[0].font.color.rgb = RGBColor(190, 35, 35)
    title = f"Отчёт по поисковому продвижению сайта {project.get('domain')} за {_month(period)}"
    doc.add_heading(_clean(title), 0)
    doc.add_paragraph(_clean(project.get("name")))
    doc.add_paragraph(f"Дата формирования: {timezone.localdate():%d.%m.%Y}")
    doc.add_page_break()
    blocks = {block.section_code: block.effective_text for block in narratives}
    engine_order = {"yandex": 0, "google": 1}
    segments = sorted(
        payload.get("calculated", {}).get("positions", {}).get("segments", []),
        key=lambda item: (
            engine_order.get(item.get("search_engine"), 99),
            item.get("region") or "",
        ),
    )
    top_mode = project.get("top_11_20_mode", "auto")
    for code in SECTION_ORDER:
        if not section_enabled(payload, code):
            continue
        if code == "top_11_20" and top_mode == "disabled":
            continue
        narrative = blocks.get(code) or "Данные раздела отсутствуют."
        if (
            code == "top_11_20"
            and top_mode == "auto"
            and not any(s.get("top_11_20") for s in segments)
        ):
            continue
        if code == "completed_work":
            # This is the final report section. Keep its heading, table and conclusion
            # in one landscape section and do not create a trailing portrait page.
            _start_landscape(doc)
        doc.add_heading(TITLES[code], level=1)
        if code == "visibility":
            _render_visibility(doc, payload, segments, narrative)
        elif code == "position_distribution":
            _render_distribution(doc, payload, segments, narrative)
        elif code in TOP_SECTION_RANGES:
            start, end = TOP_SECTION_RANGES[code]
            _render_top(doc, payload, segments, narrative, start, end, code)
        elif code == "position_dynamics":
            _render_position_dynamics(doc, payload, segments, narrative)
        elif code == "traffic_sources":
            _render_traffic_sources(doc, payload, narrative)
        elif code in {
            "traffic",
            "clicks_impressions",
            "ctr",
            "indexing",
            "iks",
            "geography",
        }:
            _render_metrics(doc, payload, code, narrative)
        elif code == "completed_work":
            _render_work(doc, payload, narrative)
        warning_messages = {
            _clean(issue.message)
            for issue in issues
            if issue.severity == "warning" and issue.section_code == code
        }
        for message in sorted(warning_messages):
            doc.add_paragraph("Предупреждение: " + message, style="Depth Note")
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def _xlsx_value(value):
    return _excel_safe(value) if isinstance(value, str) else value


def _xlsx(snapshot, draft):
    payload = snapshot.payload
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    workbook = Workbook()
    metadata = workbook.active
    metadata.title = "Метаданные"
    project = payload.get("project", {})
    period = payload.get("periods", {}).get("report", {}).get("start")
    depths = "; ".join(
        f"{s.get('search_engine')} / {s.get('region')}: TOP-{s.get('ranking_depth')}"
        for s in payload.get("calculated", {}).get("positions", {}).get("segments", [])
    )
    for row in (
        ("проект", project.get("name")),
        ("домен", project.get("domain")),
        ("месяц", date.fromisoformat(str(period)[:10])),
        ("версия", snapshot.version.number),
        ("checksum snapshot", snapshot.checksum),
        ("дата создания", timezone.localtime(snapshot.created_at).replace(tzinfo=None)),
        ("formula_version", snapshot.formula_version),
        ("глубины", depths),
        ("черновик", draft),
    ):
        metadata.append(tuple(_xlsx_value(value) for value in row))
    positions = workbook.create_sheet("Позиции")
    position_headers = [
        "Поисковая система",
        "Регион",
        "Дата",
        "Запрос",
        "Частотность",
        "Позиция",
        "Статус",
        "Группа",
        "Фактическая глубина",
    ]
    if show_urls:
        position_headers.insert(8, "Релевантный URL")
    positions.append(position_headers)
    engine_order = {"yandex": 0, "google": 1}
    ranking_sources = sorted(
        payload.get("ranking_sources", []),
        key=lambda item: (
            engine_order.get(item.get("search_engine"), 99),
            item.get("region") or "",
            item.get("date") or "",
        ),
    )
    for source in ranking_sources:
        for row in source.get("positions", []):
            values = [
                source.get("search_engine"),
                source.get("region"),
                date.fromisoformat(source["date"]),
                row.get("query"),
                row.get("frequency"),
                row.get("position"),
                row.get("status"),
                row.get("group"),
                source.get("ranking_depth"),
            ]
            if show_urls:
                values.insert(8, row.get("target_url"))
            positions.append(tuple(_xlsx_value(value) for value in values))
    history = workbook.create_sheet("История")
    history.append(("Система", "Регион", "Месяц", "Видимость", "Глубина", "Распределение"))
    for segment in payload.get("calculated", {}).get("positions", {}).get("segments", []):
        for row in segment.get("three_month_series", []):
            history.append(
                (
                    segment.get("search_engine"),
                    segment.get("region"),
                    date.fromisoformat(row["month"]),
                    row.get("visibility"),
                    row.get("ranking_depth"),
                    _excel_safe(str((row.get("distribution") or {}).get("ranges", {}))),
                )
            )
    metrics = workbook.create_sheet("Метрика и Вебмастер")
    metrics.append(("Источник", "Начало", "Конец", "Показатель", "Значение", "Единица"))
    for source in payload.get("source_snapshots", []):
        for metric in source.get("metrics", []):
            metrics.append(
                tuple(
                    _xlsx_value(value)
                    for value in (
                        source.get("source"),
                        date.fromisoformat(source["period_start"]),
                        date.fromisoformat(source["period_end"]),
                        metric.get("code"),
                        float(metric["value"]) if metric.get("value") is not None else None,
                        metric.get("unit"),
                    )
                )
            )
    traffic_facts = _metric_source(payload, "yandex_metrika").get("traffic_source_dynamics", {})
    for name, fact in traffic_facts.items():
        for point in fact.get("series", []):
            metrics.append(
                (
                    "yandex_metrika",
                    date.fromisoformat(point["month"]),
                    date.fromisoformat(point["month"]),
                    f"traffic_source_{_excel_safe(name)}_monthly_total",
                    float(point["value"]) if point.get("value") is not None else None,
                    "count",
                )
            )
    work = workbook.create_sheet("Выполненные работы")
    work_headers = ["Дата", "Категория", "Название", "Статус"]
    if show_urls:
        work_headers.extend(("Страница или материал", "URL", "Результат"))
    else:
        work_headers.append("Материал")
    work_headers.extend(("Объём", "Ответственный", "Комментарий"))
    work.append(work_headers)
    for item in payload.get("completed_work", []):
        values = [
            date.fromisoformat(item["date"]),
            item.get("category"),
            item.get("title"),
            item.get("status"),
        ]
        if show_urls:
            values.extend(
                (item.get("page_or_material_name"), item.get("url"), item.get("result_url"))
            )
        else:
            values.append(item.get("page_or_material_name"))
        values.extend((item.get("character_count"), item.get("responsible"), item.get("comment")))
        work.append(tuple(_xlsx_value(value) for value in values))
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="315B7D")
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(
                55, max(12, max(len(str(cell.value or "")) for cell in column) + 2)
            )
            for cell in column:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _pdf(docx_bytes):
    with tempfile.TemporaryDirectory(prefix="seo-export-") as temporary:
        root = Path(temporary)
        source = root / "report.docx"
        source.write_bytes(docx_bytes)
        profile = root / "lo-profile"
        profile.mkdir()
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(root),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=settings.REPORT_PDF_TIMEOUT_SECONDS,
            check=False,
        )
        pdf = root / "report.pdf"
        if (
            result.returncode
            or not pdf.exists()
            or pdf.stat().st_size == 0
            or not pdf.read_bytes().startswith(b"%PDF-")
        ):
            raise RuntimeError(f"LibreOffice conversion failed (code {result.returncode})")
        return pdf.read_bytes()


def generate_artifact(*, version, artifact_type, is_draft=False, created_by=None):
    artifact = GeneratedArtifact.objects.create(
        report_version=version,
        artifact_type=artifact_type,
        is_draft=is_draft,
        created_by=created_by,
    )
    try:
        readiness = get_publication_readiness(version)
        if readiness.has_errors and not is_draft:
            raise ExportBlocked("Финальный экспорт заблокирован ошибками валидации.")
        snapshot = ReportDatasetSnapshot.objects.select_related("version").get(version=version)
        narratives = list(
            NarrativeBlock.objects.filter(report_version=version).order_by(
                "sort_order", "created_at"
            )
        )
        issues = list(ValidationIssue.objects.filter(version=version))
        log = "; ".join(_clean(issue.message) for issue in issues if issue.severity == "warning")
        if artifact_type == "docx":
            data = _docx(snapshot, narratives, issues, is_draft)
        elif artifact_type == "xlsx":
            data = _xlsx(snapshot, is_draft)
        elif artifact_type == "pdf":
            data = _pdf(_docx(snapshot, narratives, issues, is_draft))
        else:
            raise ValueError("Unsupported artifact type")
        domain = (
            re.sub(
                r"[^a-zA-Z0-9.-]+",
                "-",
                str(snapshot.payload.get("project", {}).get("normalized_domain") or "report"),
            ).strip(".-")
            or "report"
        )
        month = str(snapshot.payload.get("periods", {}).get("report", {}).get("start"))[:7]
        suffix = "_draft" if is_draft else ""
        filename = f"{domain}_report_{month}_v{version.number}{suffix}.{artifact_type}"
        artifact.file.save(filename, ContentFile(data), save=False)
        artifact.filename = filename
        artifact.mime_type = MIMES[artifact_type]
        artifact.size = len(data)
        artifact.sha256 = hashlib.sha256(data).hexdigest()
        artifact.generation_log = log
        artifact.status = GeneratedArtifact.Status.READY
        artifact.generator_version = GENERATOR_VERSION
        artifact.save()
        return artifact
    except Exception as exc:
        artifact.status = GeneratedArtifact.Status.FAILED
        artifact.generation_log = "Экспорт не завершён (" + type(exc).__name__ + ")."
        artifact.save(update_fields=["status", "generation_log"])
        raise
