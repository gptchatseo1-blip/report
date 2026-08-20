"""Offline renderers whose business data comes only from a frozen report snapshot."""

import base64
import hashlib
import io
import math
import re
import shutil
import subprocess
import tempfile
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

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
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Patch  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from .models import GeneratedArtifact, NarrativeBlock, ReportDatasetSnapshot, ValidationIssue
from .narratives import TOP_SECTION_RANGES, section_enabled
from .validation import get_publication_readiness

GENERATOR_VERSION = "mvp1.2-provider-fidelity"
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
    "average_position": "Средняя позиция",
    "indexed_pages": "Индексируемые страницы",
    "iks": "ИКС",
    "quality_index": "ИКС",
    "geography_moscow_visits": "Москва",
    "geography_saint_petersburg_visits": "Санкт-Петербург",
    "geography_undefined_visits": "Не определено",
    "geography_area_undefined_visits": "Область не определена",
}
TOPVISOR_COLORS = {
    "visibility": "#66CC00",
    "1-3": "#2D9CDB",
    "1-10": "#15966F",
    "11-20": "#18BDA3",
    "11-30": "#18BDA3",
    "31-50": "#8CD993",
    "51-100": "#B0C8C8",
    "101+": "#FFB820",
}
WEBMASTER_COLORS = ("#FFD54A", "#8BCB55", "#FF6657", "#84BFE0")
METRIKA_COLORS = ("#7A45E5", "#FF3399", "#0FBDA0", "#3388FF", "#FFB851")
CHART_FONT = "sans-serif"


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


def _set_table_borders(table, color, *, size="4"):
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.get_or_add_tcPr()
            borders = properties.first_child_found_in("w:tcBorders")
            if borders is None:
                borders = OxmlElement("w:tcBorders")
                properties.append(borders)
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                element = borders.find(qn(f"w:{edge}"))
                if element is None:
                    element = OxmlElement(f"w:{edge}")
                    borders.append(element)
                element.set(qn("w:val"), "single")
                element.set(qn("w:sz"), size)
                element.set(qn("w:color"), color)


def _table(doc, headers, rows, widths=None, *, header_fill=None, cell_fills=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Report Table"
    table.autofit = False
    widths = widths or [16 / len(headers)] * len(headers)
    layout = table._tbl.tblPr.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        table._tbl.tblPr.append(layout)
    layout.set(qn("w:type"), "fixed")
    for grid_column, width in zip(table._tbl.tblGrid.gridCol_lst, widths, strict=True):
        grid_column.w = Cm(width)
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
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            _set_cell_width(cell, width)
            if cell_fills and row_index < len(cell_fills):
                fills = cell_fills[row_index]
                if column_index < len(fills):
                    _shade_cell(cell, fills[column_index])
    _keep_small_table_together(table)
    return table


def _save_figure(figure):
    output = io.BytesIO()
    figure.savefig(
        output,
        format="png",
        dpi=150,
        facecolor="white",
        metadata={"Software": GENERATOR_VERSION},
    )
    plt.close(figure)
    output.seek(0)
    return output


def _style_axis(axis, *, grid_axis="both"):
    axis.set_axisbelow(True)
    axis.grid(axis=grid_axis, color="#E7EBEF", linewidth=0.75)
    axis.tick_params(colors="#8B98A7", labelsize=8, length=0)
    for spine in axis.spines.values():
        spine.set_visible(False)


def _visibility_chart(points, title=None):
    useful = [(month, value) for month, value in points if value is not None]
    if not useful:
        return None
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure = plt.figure(figsize=(7.2, 3.0), dpi=150, facecolor="white")
        grid = figure.add_gridspec(1, 2, width_ratios=(3.5, 1.2), wspace=0.22)
        axis = figure.add_subplot(grid[0, 0])
        labels = [_month_short(month) for month, _value in useful]
        values = [float(value) for _month, value in useful]
        green = TOPVISOR_COLORS["visibility"]
        axis.plot(labels, values, color=green, linewidth=1.8, marker="o", markersize=3.5)
        axis.fill_between(labels, values, color=green, alpha=0.07)
        axis.set_ylim(0, 100)
        axis.set_yticks((0, 50, 100), labels=("0%", "50%", "100%"))
        _style_axis(axis)
        donut = figure.add_subplot(grid[0, 1])
        current = max(0, min(100, values[-1]))
        donut.pie(
            [current, 100 - current],
            startangle=78,
            counterclock=False,
            colors=(green, "#F0F0F0"),
            wedgeprops={"width": 0.18, "edgecolor": "white"},
        )
        donut.text(
            0,
            0,
            f"{current:.0f}%",
            ha="center",
            va="center",
            fontsize=12,
            color=green,
        )
        donut.set_aspect("equal")
        figure.subplots_adjust(left=0.08, right=0.99, top=0.98, bottom=0.16)
        return _save_figure(figure)


def _topvisor_buckets(distribution, depth):
    ranges = distribution.get("ranges") or {}
    total = distribution.get("total") or 0
    buckets = [
        ("1-3", ranges.get("1-3", 0)),
        ("1-10", distribution.get("top_10", 0)),
    ]
    if depth >= 30:
        buckets.append(("11-30", (ranges.get("11-20") or 0) + (ranges.get("21-30") or 0)))
    elif depth >= 20:
        buckets.append(("11-20", ranges.get("11-20", 0)))
    if depth >= 50:
        buckets.append(("31-50", ranges.get("31-50", 0)))
    if depth >= 100:
        buckets.append(("51-100", ranges.get("51-100", 0)))
        buckets.append(("101+", max(total - sum(ranges.values()), 0)))
    return [
        {
            "label": label,
            "count": count or 0,
            "share": Decimal(str(count or 0)) * 100 / Decimal(str(total)) if total else None,
        }
        for label, count in buckets
    ]


def _distribution_chart(history, depth):
    useful_rows = [row for row in history if (row.get("distribution") or {}).get("total")]
    if not useful_rows:
        return None
    bucket_rows = [_topvisor_buckets(row.get("distribution") or {}, depth) for row in useful_rows]
    labels = [_month_short(row.get("month")) for row in useful_rows]
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.35), dpi=150, facecolor="white")
        for index, bucket in enumerate(bucket_rows[0]):
            name = bucket["label"]
            values = [float(row[index]["share"] or 0) for row in bucket_rows]
            color = TOPVISOR_COLORS[name]
            axis.plot(
                labels,
                values,
                color=color,
                linewidth=1.7,
                marker="o",
                markersize=3.2,
                label=name,
            )
            axis.fill_between(labels, values, color=color, alpha=0.055)
        top = max(float(bucket["share"] or 0) for row in bucket_rows for bucket in row)
        axis.set_ylim(0, max(10, math.ceil(top / 10) * 10 + 2))
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.0f}%"))
        _style_axis(axis)
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=min(6, len(bucket_rows[0])),
            frameon=False,
            fontsize=8,
        )
        figure.subplots_adjust(left=0.14, right=0.98, top=0.98, bottom=0.25)
        return _save_figure(figure)


def _distribution_cards(distribution, depth):
    buckets = _topvisor_buckets(distribution, depth)
    if not buckets:
        return None
    row_count = math.ceil(len(buckets) / 2)
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(4.0, 0.67 * row_count), dpi=150, facecolor="white")
        axis.set_xlim(0, 2)
        axis.set_ylim(0, row_count)
        axis.axis("off")
        for index, bucket in enumerate(buckets):
            column = index // row_count
            row = index % row_count
            x = column + 0.03
            y = row_count - row - 0.9
            axis.add_patch(
                FancyBboxPatch(
                    (x, y),
                    0.91,
                    0.78,
                    boxstyle="round,pad=0.02,rounding_size=0.08",
                    linewidth=0,
                    facecolor="#F0F2F5",
                )
            )
            axis.add_patch(
                FancyBboxPatch(
                    (x + 0.04, y + 0.12),
                    0.28,
                    0.52,
                    boxstyle="round,pad=0.02,rounding_size=0.05",
                    linewidth=0,
                    facecolor=TOPVISOR_COLORS[bucket["label"]],
                )
            )
            axis.text(
                x + 0.18,
                y + 0.38,
                bucket["label"],
                ha="center",
                va="center",
                color="white",
                fontsize=8,
                fontweight="bold",
            )
            axis.text(
                x + 0.43,
                y + 0.38,
                _number(bucket["share"], "%", decimal_places=0),
                ha="left",
                va="center",
                color="#91A0AF",
                fontsize=8,
            )
            axis.text(
                x + 0.82,
                y + 0.38,
                _number(bucket["count"]),
                ha="right",
                va="center",
                color="#5D6875",
                fontsize=8,
            )
        figure.subplots_adjust(left=0, right=1, top=1, bottom=0)
        return _save_figure(figure)


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


def _add_report_picture(doc, picture, *, width=16.5):
    if picture is None:
        doc.add_paragraph("Данные недоступны для построения графика.", style="Data Missing")
        return False
    doc.add_picture(picture, width=Cm(width))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    return True


def _position_fill(position):
    if position is None:
        return None
    if position <= 3:
        return "80DFA8"
    if position <= 5:
        return "A7E8C1"
    if position <= 10:
        return "D9F3E3"
    if position <= 20:
        return "EDF8F1"
    return "F6FBF8"


def _render_position_table(doc, payload, segment, start, end):
    source = _current_position_source(payload, segment)
    rows = _position_rows(source, start, end, show_urls=False)
    engine = segment.get("search_engine")
    engine_header = "Yandex" if engine == "yandex" else "Google"
    if not rows:
        doc.add_paragraph("Запросы в выбранном диапазоне отсутствуют.", style="Data Missing")
        return False
    widths = [7.74, 1.50, 1.56, 8.25] if engine == "yandex" else [8.63, 1.17, 1.52, 7.42]
    cell_fills = []
    for row in rows:
        cell_fills.append(("E7E7E7", None, _position_fill(row[2]), None))
    table = _table(
        doc,
        ("Запросы", "WS", engine_header, "Имя группы"),
        rows,
        widths,
        header_fill="EEF1F2",
        cell_fills=cell_fills,
    )
    for cell in table.rows[0].cells:
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in cell.paragraphs[0].runs:
            run.bold = True
            run.font.size = Pt(7.5)
    for row in table.rows[1:]:
        for cell in row.cells:
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(8)
        for column in (1, 2):
            row.cells[column].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    return True


def _monthly_topvisor_rows(segment):
    rows = []
    depth = segment.get("ranking_depth") or 0
    for point in segment.get("three_month_series") or []:
        distribution = point.get("distribution") or {}
        buckets = {item["label"]: item for item in _topvisor_buckets(distribution, depth)}

        def bucket(label, bucket_values=buckets):
            item = bucket_values.get(label) or {}
            return (
                f"{_number(item.get('share'), '%', decimal_places=0)} "
                f"({_number(item.get('count'))})"
            )

        final_label = "11-30" if depth >= 30 else "11-20"
        rows.append(
            (
                _month(point.get("month")).capitalize(),
                _number(point.get("visibility"), "%", decimal_places=0),
                bucket("1-3"),
                bucket("1-10"),
                bucket(final_label),
            )
        )
    return rows


def _render_monthly_topvisor_table(doc, segment):
    final_label = "11-30" if (segment.get("ranking_depth") or 0) >= 30 else "11-20"
    rows = _monthly_topvisor_rows(segment)
    if not rows:
        doc.add_paragraph("Месячные итоги отсутствуют.", style="Data Missing")
        return
    table = _table(
        doc,
        ("Месяц", "Видимость", "в топ 3", "в топ 10", f"в топ {final_label}"),
        rows,
        [3.7, 3.7, 3.7, 3.7, 3.7],
        header_fill="EEF1F2",
    )
    for row in table.rows:
        for column in range(1, 5):
            row.cells[column].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER


def _comparison_phrase(current, previous):
    if current is None or previous is None:
        return "нет базы сравнения"
    delta = Decimal(str(current)) - Decimal(str(previous))
    if delta == 0:
        return "не изменилось"
    direction = "увеличилось" if delta > 0 else "уменьшилось"
    return f"{direction} на {_number(abs(delta), decimal_places=1)} п. п."


def _relative_comparison_phrase(current, previous):
    if current is None or previous is None:
        return "нет базы сравнения"
    current_number = Decimal(str(current))
    previous_number = Decimal(str(previous))
    if current_number == previous_number:
        return "не изменилось"
    if previous_number == 0:
        return "изменение не рассчитывается из-за нулевой базы"
    delta = (current_number - previous_number) / abs(previous_number) * Decimal(100)
    direction = "увеличилось" if delta > 0 else "уменьшилось"
    return f"{direction} на {_number(abs(delta), '%', decimal_places=0)}"


def _render_topvisor_comparison(doc, segment):
    history = segment.get("three_month_series") or []
    if not history:
        return
    current = history[-1]
    previous = history[-2] if len(history) >= 2 else None
    depth = segment.get("ranking_depth") or 0
    current_buckets = {
        item["label"]: item for item in _topvisor_buckets(current.get("distribution") or {}, depth)
    }
    previous_buckets = (
        {
            item["label"]: item
            for item in _topvisor_buckets(previous.get("distribution") or {}, depth)
        }
        if previous
        else {}
    )
    final_label = "11-30" if depth >= 30 else "11-20"
    doc.add_paragraph("В сравнении с прошлым месяцем доля запросов в топ:")
    for label in ("1-3", "1-10", final_label):
        now = (current_buckets.get(label) or {}).get("share")
        before = (previous_buckets.get(label) or {}).get("share")
        doc.add_paragraph(
            f"Запросов в топ {label} — {_number(now, '%', decimal_places=0)} "
            f"({_relative_comparison_phrase(now, before)}).",
            style="Compact",
        )
    now_visibility = current.get("visibility")
    previous_visibility = previous.get("visibility") if previous else None
    engine = ENGINE_LABELS.get(segment.get("search_engine"), "Поиск")
    region = segment.get("region") or "регион не указан"
    doc.add_paragraph(
        f"Общая видимость сайта в ПС {engine}.{region} — "
        f"{_number(now_visibility, '%', decimal_places=0)} "
        f"({_relative_comparison_phrase(now_visibility, previous_visibility)}).",
        style="Compact",
    )


def _top_table_title(segment, start, end):
    range_label = f"TOP-{end}" if start == 1 else f"TOP-{start}–{end}"
    engine = ENGINE_LABELS.get(segment.get("search_engine"), "Поиск")
    region = segment.get("region") or "регион не указан"
    return f"Запросы в {range_label} по {engine}.{region}"


def _render_topvisor_segment(doc, payload, segment, blocks, *, show_link=False):
    engine = ENGINE_LABELS.get(segment.get("search_engine"), "Поиск")
    region = segment.get("region") or "регион не указан"
    doc.add_heading(f"{engine}. {region}", level=2)
    history = segment.get("three_month_series") or []
    depth = segment.get("ranking_depth") or 0
    if section_enabled(payload, "visibility"):
        doc.add_paragraph("График видимости сайта по основным ключевым словам за отчётный период.")
        _add_report_picture(
            doc,
            _visibility_chart([(point.get("month"), point.get("visibility")) for point in history]),
        )
        doc.add_paragraph(
            "Видимость сайта — это доля показов сайта в поисковых системах, которая "
            "зависит от частот и позиций запросов."
        )
    doc.add_paragraph("В распределении по топам:")
    _add_report_picture(doc, _distribution_chart(history, depth))
    doc.add_paragraph(
        "Данная диаграмма не отражает зависимости запросов от частот и отражает только "
        "количество запросов в топ 3, топ 10, топ 30 и пр."
    )
    doc.add_paragraph("Данные по количеству запросов в топ:")
    _add_report_picture(
        doc,
        _distribution_cards(segment.get("distribution") or {}, depth),
        width=8.0,
    )
    if section_enabled(payload, "position_dynamics"):
        doc.add_paragraph("В динамике по месяцам")
        _render_monthly_topvisor_table(doc, segment)
    _render_topvisor_comparison(doc, segment)
    if show_link:
        options = payload.get("display_options", {})
        doc.add_paragraph(
            "Подробный отчёт доступен по ссылке: " + _clean(options["topvisor_report_url"])
        )
    top_mode = payload.get("project", {}).get("top_11_20_mode", "auto")
    for code in ("top_5", "top_10", "top_20", "top_11_30", "top_30", "top_11_20"):
        if not section_enabled(payload, code):
            continue
        if code == "top_11_20" and top_mode == "disabled":
            continue
        start, end = TOP_SECTION_RANGES[code]
        if code == "top_11_20" and top_mode == "auto":
            source = _current_position_source(payload, segment)
            if not _position_rows(source, start, end, show_urls=False):
                continue
        doc.add_paragraph(_top_table_title(segment, start, end), style="Table Heading")
        rendered = _render_position_table(doc, payload, segment, start, end)
        comment = blocks.get(code)
        if rendered and comment and comment != "Данные раздела отсутствуют.":
            doc.add_paragraph(_clean(comment), style="Table Comment")
    if segment.get("search_engine") == "google" and depth:
        doc.add_paragraph(
            f"Глубина проверки Google — TOP-{depth}. Для запросов за пределами "
            "подтверждённой глубины точная позиция не определяется.",
            style="Depth Note",
        )


def _render_topvisor(doc, payload, segments, blocks):
    if not segments:
        return
    doc.add_heading(
        "1) Видимость сайта в поисковых системах Яндекс и Google по основным ключевым словам",
        level=1,
    )
    options = payload.get("display_options", {})
    link_pending = bool(
        options.get("include_topvisor_report_link") and options.get("topvisor_report_url")
    )
    yandex_indexes = [i for i, item in enumerate(segments) if item.get("search_engine") == "yandex"]
    for index, segment in enumerate(segments):
        show_link = link_pending and (
            index == yandex_indexes[-1] if yandex_indexes else index == len(segments) - 1
        )
        _render_topvisor_segment(doc, payload, segment, blocks, show_link=show_link)
        link_pending = link_pending and not show_link


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


def _period_details(payload, source):
    return _metric_source(payload, source).get("period_details") or []


def _webmaster_chart_details(payload):
    details = _period_details(payload, "yandex_webmaster")
    if not details:
        return []
    if payload.get("display_options", {}).get("webmaster_chart_period") == "selected":
        return details
    return details[-1:]


def _detail_payload(detail):
    return detail.get("payload") or {}


def _daily_rows(details, key):
    rows = []
    for detail in details:
        rows.extend((_detail_payload(detail).get("daily") or {}).get(key, []))
    return sorted(rows, key=lambda item: item.get("date") or "")


def _date_label(value):
    parsed = date.fromisoformat(str(value)[:10])
    return f"{parsed.day} {MONTHS[parsed.month - 1]}"


def _period_caption(doc, rows, *, detail="по дням"):
    dates = [row.get("date") for row in rows if row.get("date")]
    if not dates:
        return
    start, end = min(dates), max(dates)
    doc.add_paragraph(
        f"Период: {date.fromisoformat(start):%d.%m.%Y} — "
        f"{date.fromisoformat(end):%d.%m.%Y}. Детализация: {detail}.",
        style="Chart Caption",
    )


def _nice_step(values, *, minimum=10):
    if not values:
        return minimum
    spread = max(values) - min(values)
    raw = max(minimum, spread / 4)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw else 1
    normalized = raw / magnitude
    factor = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    return max(minimum, factor * magnitude)


def _single_service_chart(points, *, title, color, fill=False, suffix="", minimum_step=None):
    useful = [(day, value) for day, value in points if value is not None]
    if not useful:
        return None
    labels = [_date_label(day) for day, _ in useful]
    values = [float(value) for _, value in useful]
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.25), dpi=150, facecolor="white")
        axis.plot(labels, values, linewidth=2.0, color=color)
        if fill:
            axis.fill_between(labels, values, color=color, alpha=0.82)
        axis.set_title(title, loc="left", fontsize=13, color="#2F343B", pad=16)
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:g}{suffix}"))
        if minimum_step:
            step = _nice_step(values, minimum=minimum_step)
            low = max(0, math.floor(min(values) / step) * step - step)
            high = math.ceil(max(values) / step) * step + step
            if high <= low:
                high = low + step * 2
            axis.set_ylim(low, high)
            axis.set_yticks([low + step * index for index in range(int((high - low) / step) + 1)])
        _style_axis(axis)
        axis.xaxis.set_major_locator(MaxNLocator(7))
        figure.tight_layout()
        return _save_figure(figure)


def _webmaster_search_chart(payload):
    configs = (
        ("Показы", "shows", "#8BCB55"),
        ("Клики", "clicks", "#F7C933"),
        ("CTR, %", "ctr", "#FF6657"),
        ("Ср. позиция", "average_position", "#84BFE0"),
    )
    rows = _daily_rows(_webmaster_chart_details(payload), "queries")
    if not rows:
        return None
    labels = [_date_label(row["date"]) for row in rows]
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.55), dpi=150, facecolor="white")
        for index, row in enumerate(rows):
            if date.fromisoformat(row["date"]).weekday() >= 5:
                axis.axvspan(index - 0.5, index + 0.5, color="#F1F1F1", zorder=0)
        for label, code, color in configs:
            raw = [float(row[code]) if row.get(code) is not None else float("nan") for row in rows]
            finite = [value for value in raw if not math.isnan(value)]
            if not finite:
                continue
            low, high = min(finite), max(finite)
            values = [
                (value - low) / (high - low) if high > low and not math.isnan(value) else 0.5
                for value in raw
            ]
            axis.plot(labels, values, linewidth=1.4, color=color, label=label)
        axis.set_title(
            "Показы, клики, CTR и средняя позиция",
            loc="left",
            fontsize=13,
            color="#2F343B",
            pad=16,
        )
        _style_axis(axis)
        axis.set_yticks([])
        axis.xaxis.set_major_locator(MaxNLocator(8))
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.13),
            ncol=4,
            frameon=False,
            fontsize=8,
        )
        figure.subplots_adjust(left=0.04, right=0.98, top=0.87, bottom=0.23)
        return _save_figure(figure)


def _change_table(doc, payload, source, codes, *, provider):
    table = _table(
        doc,
        (
            "Показатель",
            "Предыдущий период",
            "Текущий период",
            "Абсолютное изменение",
            "Относительное изменение",
        ),
        _change_rows(payload, source, codes),
        [5.4, 3.0, 3.0, 3.4, 3.7],
        header_fill="F5F7FA",
    )
    for row_index, row in enumerate(table.rows[1:], start=0):
        change = (_metric_source(payload, source).get("normalized_changes") or {}).get(
            codes[row_index]
        ) or {}
        delta = change.get("absolute")
        if delta is None:
            continue
        color = (
            "26A95B"
            if Decimal(str(delta)) > 0
            else "F04444"
            if Decimal(str(delta)) < 0
            else "7A8796"
        )
        for column in (3, 4):
            for run in row.cells[column].paragraphs[0].runs:
                run.font.color.rgb = RGBColor.from_string(color)
    _set_table_borders(table, "E1E7EE", size="3")
    return table


def _latest_webmaster_payload(payload):
    details = _period_details(payload, "yandex_webmaster")
    return _detail_payload(details[-1]) if details else {}


def _decimal_or_none(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _relative_delta(current, previous):
    current = _decimal_or_none(current)
    previous = _decimal_or_none(previous)
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * Decimal(100)


def _provider_value(code, value):
    if code in {"ctr", "average_position", "bounce_rate", "conversion_rate"}:
        return _number(value, decimal_places=2)
    return _number(value, decimal_places=0)


def _change_color(current, previous, *, lower_is_better=False):
    current = _decimal_or_none(current)
    previous = _decimal_or_none(previous)
    if current is None or previous is None or current == previous:
        return "7A8796"
    improved = current < previous if lower_is_better else current > previous
    return "26A95B" if improved else "F04444"


def _paired_metric_cell(cell, code, current, previous, *, lower_is_better=False):
    cell.text = _provider_value(code, current)
    cell.paragraphs[0].runs[0].font.size = Pt(9)
    if previous is None:
        return
    delta = abs((_decimal_or_none(current) or 0) - (_decimal_or_none(previous) or 0))
    change_text = _number(
        delta,
        decimal_places=2 if code in {"ctr", "average_position", "bounce_rate"} else 0,
    )
    change = cell.add_paragraph(change_text)
    change.paragraph_format.space_after = Pt(0)
    change.runs[0].font.size = Pt(7.5)
    change.runs[0].font.color.rgb = RGBColor.from_string(
        _change_color(current, previous, lower_is_better=lower_is_better)
    )


def _webmaster_query_summary_table(doc, current, previous):
    headers = ("Группа запросов", "Показы", "Клики", "CTR, %", "Ср. позиция")
    codes = ("shows", "clicks", "ctr", "average_position")
    table = _table(
        doc,
        headers,
        [("Все запросы", "", "", "", "")],
        [6.4, 3.0, 3.0, 2.8, 3.3],
        header_fill="F7F7F7",
    )
    for index, code in enumerate(codes, start=1):
        _paired_metric_cell(
            table.rows[1].cells[index],
            code,
            current.get(code),
            previous.get(code),
            lower_is_better=code == "average_position",
        )
    _set_table_borders(table, "D7DADF", size="3")
    return table


def _webmaster_query_text(current, previous):
    shows = _relative_delta(current.get("shows"), previous.get("shows"))
    clicks = _relative_delta(current.get("clicks"), previous.get("clicks"))
    ctr_current = _decimal_or_none(current.get("ctr"))
    ctr_previous = _decimal_or_none(previous.get("ctr"))
    position_current = _decimal_or_none(current.get("average_position"))
    position_previous = _decimal_or_none(previous.get("average_position"))

    def movement(value):
        if value is None:
            return "не рассчитано"
        return (
            ("увеличилось" if value >= 0 else "снизилось")
            + " на "
            + _number(abs(value), "%", decimal_places=0)
        )

    ctr_delta = (
        ctr_current - ctr_previous if ctr_current is not None and ctr_previous is not None else None
    )
    position_delta = (
        position_current - position_previous
        if position_current is not None and position_previous is not None
        else None
    )
    ctr_text = (
        ("увеличилось" if ctr_delta >= 0 else "снизилось")
        + " на "
        + _number(abs(ctr_delta), "%", decimal_places=2)
        if ctr_delta is not None
        else "не рассчитано"
    )
    position_text = (
        ("улучшении" if position_delta < 0 else "снижении")
        + " средней позиции по всем запросам на "
        + _number(abs(position_delta), decimal_places=2)
        + " пункта"
        if position_delta is not None
        else "недоступной динамике средней позиции"
    )
    return (
        "По сравнению с предыдущим аналогичным периодом (сравнивается с периодом, "
        "равным по количеству дней с текущим) общее количество показов по всем запросам "
        f"в поисковой системе Яндекс {movement(shows)}. Количество кликов {movement(clicks)}. "
        f"Значение CTR {ctr_text} при {position_text}."
    )


def _webmaster_popular_table(doc, current_rows, previous_rows):
    previous = {row.get("query", "").casefold(): row for row in previous_rows}
    table = _table(
        doc,
        ("Запрос", "Показы", "Клики", "CTR, %", "Ср. позиция"),
        [(row.get("query"), "", "", "", "") for row in current_rows[:20]],
        [8.7, 2.7, 2.4, 2.4, 2.8],
        header_fill="F7F7F7",
    )
    for row_index, current in enumerate(current_rows[:20], start=1):
        before = previous.get(current.get("query", "").casefold(), {})
        for column, code in enumerate(("shows", "clicks", "ctr", "average_position"), start=1):
            _paired_metric_cell(
                table.rows[row_index].cells[column],
                code,
                current.get(code),
                before.get(code),
                lower_is_better=code == "average_position",
            )
    _set_table_borders(table, "D7DADF", size="3")
    return table


def _add_uploaded_picture(doc, payload):
    image = payload.get("display_options", {}).get("webmaster_queries_screenshot") or {}
    if not image.get("data"):
        return False
    try:
        data = io.BytesIO(base64.b64decode(image["data"], validate=True))
    except (ValueError, TypeError):
        return False
    _add_report_picture(doc, data)
    return True


def _render_indexing_legend(doc, distribution):
    rows = (distribution or {}).get("rows") or []
    if not rows:
        return
    paragraph = doc.add_paragraph()
    colors = ("00B945", "15A850", "3BBB69", "69C985", "8BD6A1", "A9DFB8")
    for index, row in enumerate(rows):
        color = "F2B51D" if row.get("path") == "Статус неизвестен" else colors[index % len(colors)]
        count = paragraph.add_run(_number(row.get("count"), decimal_places=0) + "  ")
        count.bold = True
        count.font.color.rgb = RGBColor.from_string(color)
        paragraph.add_run(str(row.get("path") or "") + "    ")


def _render_iks_explanation(doc):
    doc.add_paragraph(
        "Индекс качества сайта — это показатель того, насколько полезен сайт для пользователей "
        "с точки зрения Яндекса. Данный показатель напрямую не влияет на ранжирование сайтов "
        "в органической выдаче."
    )
    doc.add_paragraph(
        "При расчете индекса качества учитываются размер аудитории сайта, степень "
        "удовлетворенности пользователей, уровень доверия к сайту со стороны пользователей "
        "и Яндекса, а также множество других факторов, которые Яндекс не разглашает. Для "
        "расчета используются данные сервисов Яндекса. Значение ИКС регулярно пересчитывается "
        "с учетом множества факторов. За время между пересчетами, связанные с сайтом факторы "
        "могут измениться."
    )
    doc.add_paragraph("Для увеличения показателя ИКС необходимо:")
    for text in (
        "развивать популярность бренда как онлайн, так и офлайн;",
        "увеличивать трафик на сайт со всех возможных источников;",
        "расширять структуру сайта и семантику, задействовать максимальное количество URL, "
        "генерирующих трафик;",
        "работать над вовлеченностью пользователями ресурсом.",
    ):
        doc.add_paragraph(text, style="List Bullet")
    doc.add_paragraph(
        "Значение ИКС регулярно пересчитывается с учетом множества факторов, в том числе "
        "сезонности. За время между пересчетами, связанные с сайтом факторы могут измениться."
    )


def _render_webmaster(doc, payload, blocks):
    enabled = [
        code
        for code in ("iks", "indexing", "clicks_impressions", "ctr")
        if section_enabled(payload, code)
    ]
    if not enabled:
        return
    doc.add_heading("2) Индексация сайта (Яндекс.Вебмастер)", level=1)
    source = "yandex_webmaster"
    details = _webmaster_chart_details(payload)
    latest = _latest_webmaster_payload(payload)
    if "iks" in enabled:
        codes = tuple(
            code for code in ("iks", "quality_index") if _metric_has_data(payload, source, code)
        )[:1]
        if codes:
            code = codes[0]
            change = (_metric_source(payload, source).get("normalized_changes") or {}).get(
                code
            ) or {}
            current = change.get("current")
            previous = change.get("previous")
            delta = (
                abs(Decimal(str(current)) - Decimal(str(previous)))
                if current is not None and previous is not None
                else None
            )
            direction = None
            if current is not None and previous is not None:
                direction = (
                    "увеличилось"
                    if Decimal(str(current)) > Decimal(str(previous))
                    else "уменьшилось"
                )
            doc.add_paragraph(
                f"В отчётном месяце значение ИКС сайта — {_number(current)} единиц "
                + (
                    f"({direction} на {_number(delta, decimal_places=0)})."
                    if delta not in (None, 0)
                    else "(нет базы сравнения)."
                    if delta is None
                    else "(не изменилось)."
                )
            )
            iks_rows = _daily_rows(details, "iks")
            _period_caption(doc, iks_rows)
            _add_report_picture(
                doc,
                _single_service_chart(
                    [(row.get("date"), row.get("value")) for row in iks_rows]
                    or _metric_series(payload, source, (code,))[0][1],
                    title=f"Индекс качества сайта (ИКС) — {_number(current)}",
                    color=WEBMASTER_COLORS[0],
                    minimum_step=10,
                ),
            )
            _render_iks_explanation(doc)
    if "indexing" in enabled:
        doc.add_paragraph("Динамика количества страниц в поиске:", style="Table Heading")
        indexing_rows = _daily_rows(details, "indexed_pages")
        _period_caption(doc, indexing_rows)
        points = [(row.get("date"), row.get("value")) for row in indexing_rows]
        _add_report_picture(
            doc,
            _single_service_chart(
                points or _metric_series(payload, source, ("indexed_pages",))[0][1],
                title="Страницы в поиске",
                color="#00B945",
                fill=True,
            ),
        )
        distribution = latest.get("path_distribution")
        _render_indexing_legend(doc, distribution)
        distribution_rows = (distribution or {}).get("rows") or []
        known_rows = [row for row in distribution_rows if row.get("path") != "Статус неизвестен"]
        if known_rows:
            leader = max(known_rows, key=lambda row: row.get("count") or 0)
            doc.add_paragraph(
                f"Преимущественно в поиске находятся страницы раздела {leader.get('path')} — "
                f"{_number(leader.get('count'), decimal_places=0)} URL."
            )
    if "clicks_impressions" in enabled or "ctr" in enabled:
        doc.add_paragraph(
            "Данные по показам, кликам и CTR по всем запросам:", style="Table Heading"
        )
        query_rows = _daily_rows(details, "queries")
        _period_caption(doc, query_rows)
        _add_report_picture(doc, _webmaster_search_chart(payload))
        current_summary = latest.get("query_summary") or {}
        previous_summary = latest.get("comparison_query_summary") or {}
        if current_summary:
            _webmaster_query_summary_table(doc, current_summary, previous_summary)
        else:
            codes = tuple(
                code
                for code in (
                    "search_impressions",
                    "search_clicks",
                    "search_ctr",
                    "average_position",
                )
                if _metric_has_data(payload, source, code)
            )
            if codes:
                _change_table(doc, payload, source, codes, provider="webmaster")
        doc.add_paragraph(
            "Красным шрифтом представлены цифры, показывающие спад показателя по сравнению "
            "с предыдущим периодом, зелёным — рост. Запросы, по которым начинает показываться "
            "сайт, появляются неравномерно: сначала брендовые — наиболее кликабельные, а затем "
            "показываются небрендовые (они менее кликабельные), которые появляются на более "
            "низких позициях. Поэтому показатели CTR и позиции могут снижаться."
        )
        if current_summary and previous_summary:
            doc.add_paragraph(_webmaster_query_text(current_summary, previous_summary))
    if section_enabled(payload, "webmaster_popular_queries"):
        doc.add_paragraph("Самые кликабельные запросы:", style="Table Heading")
        current_queries = latest.get("popular_queries") or []
        previous_queries = latest.get("comparison_popular_queries") or []
        if current_queries:
            _webmaster_popular_table(doc, current_queries, previous_queries)
        elif not _add_uploaded_picture(doc, payload):
            doc.add_paragraph(
                "API не вернул список популярных запросов, и скриншот не загружен.",
                style="Data Missing",
            )
        comment = blocks.get("webmaster_popular_queries") or payload.get("display_options", {}).get(
            "webmaster_queries_comment"
        )
        doc.add_paragraph(
            _clean(comment)
            if comment
            else (
                "Запросы в таблице отсортированы по кликабельности. Красным шрифтом "
                "представлены цифры, показывающие спад показателя по сравнению с предыдущим "
                "периодом, зелёным — рост, серым — оставшиеся без изменений."
            )
        )


def _metrika_comparison_chart(payload, codes, *, title):
    changes = _metric_source(payload, "yandex_metrika").get("normalized_changes", {})
    rows = [(code, changes.get(code) or {}) for code in codes]
    rows = [item for item in rows if item[1].get("current") is not None]
    if not rows:
        return None
    labels = [METRIC_LABELS.get(code, code) for code, _ in rows]
    previous = [float(change.get("previous") or 0) for _, change in rows]
    current = [float(change.get("current") or 0) for _, change in rows]
    x = list(range(len(labels)))
    width = 0.34
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.55), dpi=150, facecolor="white")
        previous_bars = axis.bar(
            [value - width / 2 for value in x],
            previous,
            width=width,
            color=[METRIKA_COLORS[i % len(METRIKA_COLORS)] for i in x],
            label="Предыдущий период",
        )
        current_bars = axis.bar(
            [value + width / 2 for value in x],
            current,
            width=width,
            color="white",
            edgecolor=[METRIKA_COLORS[i % len(METRIKA_COLORS)] for i in x],
            hatch="////",
            linewidth=1.2,
            label="Текущий период",
        )
        for index, bar in enumerate(current_bars):
            bar.set_edgecolor(METRIKA_COLORS[index % len(METRIKA_COLORS)])
        axis.set_xticks(x, labels)
        axis.set_title(title, loc="left", fontsize=13, color="#30343B")
        _style_axis(axis, grid_axis="y")
        axis.legend(
            handles=(previous_bars[0], current_bars[0]),
            labels=("Предыдущий период", "Текущий период"),
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=2,
            frameon=False,
        )
        figure.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.27)
        return _save_figure(figure)


def _metrika_source_label(name):
    return {
        "search": "Переходы из поисковых систем",
        "direct": "Прямые заходы",
        "referral": "Переходы по ссылкам на сайтах",
        "ad": "Переходы по рекламе",
        "ads": "Переходы по рекламе",
        "internal": "Внутренние переходы",
    }.get(name, name)


def _metrika_sources_chart(facts):
    order = ("search", "direct", "referral", "ad", "ads", "internal")
    color_map = {
        "search": "#7A45E5",
        "direct": "#FF3399",
        "referral": "#0FBDA0",
        "ad": "#3388FF",
        "ads": "#3388FF",
        "internal": "#FFB851",
    }
    names = [name for name in order if name in facts]
    names.extend(name for name in facts if name not in names)
    useful = [
        (
            name,
            [(point.get("month"), point.get("value")) for point in facts[name].get("series", [])],
        )
        for name in names
        if any(point.get("value") is not None for point in facts[name].get("series", []))
    ]
    if not useful:
        return None
    labels = [_month_short(month) for month, _ in useful[0][1]]
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.55), dpi=150, facecolor="white")
        for index, (name, points) in enumerate(useful):
            values = [float(value) if value is not None else float("nan") for _, value in points]
            change = facts[name].get("change") or {}
            current = change.get("current")
            relative = change.get("relative_percent")
            movement = (
                f"; {'+' if Decimal(str(relative)) > 0 else ''}"
                f"{_number(relative, '%', decimal_places=1)}"
                if relative is not None
                else ""
            )
            legend_label = (
                f"{_metrika_source_label(name)} — {_number(current)}{movement}"
                if current is not None
                else _metrika_source_label(name)
            )
            axis.plot(
                labels,
                values,
                linewidth=1.8,
                color=color_map.get(name, METRIKA_COLORS[index % len(METRIKA_COLORS)]),
                label=legend_label,
            )
        axis.set_title("Источники, сводка", loc="left", fontsize=13, color="#30343B")
        axis.set_ylabel("Визиты", color="#677485")
        _style_axis(axis)
        axis.legend(
            loc="upper center",
            bbox_to_anchor=(0.5, -0.15),
            ncol=2,
            frameon=False,
            fontsize=8,
        )
        figure.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.27)
        return _save_figure(figure)


def _metrika_period_rows(payload, key):
    robotness = payload.get("display_options", {}).get("metrika_robotness", "humans")
    periods = []
    for detail in _period_details(payload, "yandex_metrika"):
        source = _detail_payload(detail)
        variant = (source.get("search_details") or {}).get(robotness) or source
        periods.append(
            {
                "period_start": detail.get("period_start"),
                "period_end": detail.get("period_end"),
                "rows": variant.get(key) or [],
                "payload": source,
            }
        )
    return periods


def _row_dimension(row, index):
    dimensions = row.get("dimensions") or []
    return dimensions[index] if index < len(dimensions) else {"id": "", "name": ""}


def _aggregate_detail_rows(rows, key):
    result = {}
    for row in rows:
        label = key(row)
        if not label:
            continue
        target = result.setdefault(
            label,
            {"visits": Decimal(0), "users": Decimal(0), "bounce_sum": Decimal(0)},
        )
        visits = _decimal_or_none(row.get("visits")) or Decimal(0)
        users = _decimal_or_none(row.get("users")) or Decimal(0)
        bounce = _decimal_or_none(row.get("bounce_rate")) or Decimal(0)
        target["visits"] += visits
        target["users"] += users
        target["bounce_sum"] += bounce * visits
    for values in result.values():
        values["bounce_rate"] = (
            values.pop("bounce_sum") / values["visits"] if values["visits"] else Decimal(0)
        )
    return result


def _search_engine_name(row):
    dimension = _row_dimension(row, 0)
    raw = f"{dimension.get('id', '')} {dimension.get('name', '')}".casefold()
    if "yandex" in raw or "яндекс" in raw:
        return "Яндекс"
    if "google" in raw:
        return "Google"
    return str(dimension.get("name") or dimension.get("id") or "").strip()


def _compact_number(value):
    number = _decimal_or_none(value)
    if number is None:
        return "—"
    if abs(number) >= 1000:
        return f"{_number(number / Decimal(1000), decimal_places=1)} тыс."
    return _number(number, decimal_places=0)


def _metrika_search_quarter_chart(periods):
    if not periods:
        return None
    aggregates = [
        _aggregate_detail_rows(period["rows"], _search_engine_name) for period in periods[-3:]
    ]
    latest = aggregates[-1]
    engines = sorted(
        latest,
        key=lambda label: latest[label].get("visits") or Decimal(0),
        reverse=True,
    )[:3]
    if not engines:
        return None
    labels = []
    for period in periods[-3:]:
        parsed = date.fromisoformat(str(period["period_start"])[:10])
        labels.append(MONTHS[parsed.month - 1][:3])
    series = [
        (
            "Всего",
            [
                sum(
                    (values.get("visits") or Decimal(0) for values in aggregate.values()),
                    Decimal(0),
                )
                for aggregate in aggregates
            ],
            "#A79BFF",
        )
    ]
    colors = {"Google": "#7A45E5", "Яндекс": "#FF3399"}
    for index, engine in enumerate(engines):
        series.append(
            (
                engine,
                [aggregate.get(engine, {}).get("visits") or Decimal(0) for aggregate in aggregates],
                colors.get(engine, METRIKA_COLORS[(index + 2) % len(METRIKA_COLORS)]),
            )
        )
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.45), dpi=150, facecolor="white")
        for label, values, color in series:
            axis.plot(
                labels,
                [float(value) for value in values],
                color=color,
                linewidth=1.6,
                label=label,
            )
        axis.set_title("Визиты", loc="left", fontsize=11, color="#30343B")
        axis.yaxis.set_major_formatter(
            FuncFormatter(lambda value, _position: _compact_number(value))
        )
        axis.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        _style_axis(axis)
        legend_handles = [
            Line2D(
                [0],
                [0],
                marker="s",
                linestyle="none",
                markersize=5,
                markerfacecolor="#AAB4C0",
                markeredgecolor="#AAB4C0",
                label=f"Выбрано {len(series)} из {len(series)}",
            )
        ]
        legend_handles.extend(
            Line2D(
                [0],
                [0],
                marker="o",
                linestyle="none",
                markersize=5,
                markerfacecolor=color,
                markeredgecolor=color,
                label=f"{label}  {_compact_number(values[-1])}",
            )
            for label, values, color in series
        )
        axis.legend(
            handles=legend_handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=min(5, len(legend_handles)),
            frameon=False,
            fontsize=7.5,
            handletextpad=0.35,
            columnspacing=1.1,
        )
        figure.subplots_adjust(left=0.1, right=0.98, top=0.9, bottom=0.27)
        return _save_figure(figure)


def _metrika_comparison_bars(rows, *, title):
    useful = [row for row in rows if row.get("current") is not None]
    if not useful:
        return None
    x = list(range(len(useful)))
    width = 0.34
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.55), dpi=150, facecolor="white")
        for index, row in enumerate(useful):
            color = row.get("color") or METRIKA_COLORS[index % len(METRIKA_COLORS)]
            axis.bar(
                index - width / 2,
                float(row.get("previous") or 0),
                width=width,
                color=color,
                edgecolor=color,
                linewidth=0.5,
            )
            axis.bar(
                index + width / 2,
                float(row.get("current") or 0),
                width=width,
                color=color,
                alpha=0.72,
                hatch="////",
                edgecolor="#FFFFFF",
                linewidth=0.5,
            )
        axis.set_xticks(x, ["" for _row in useful])
        axis.set_title(title, loc="left", fontsize=13, color="#30343B")
        axis.yaxis.set_major_formatter(
            FuncFormatter(lambda value, _position: _compact_number(value))
        )
        axis.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
        _style_axis(axis, grid_axis="y")
        handles = []
        labels = []
        for index, row in enumerate(useful):
            color = row.get("color") or METRIKA_COLORS[index % len(METRIKA_COLORS)]
            handles.append(Patch(facecolor=color, edgecolor=color))
            labels.append(
                f"{row['label']} — {_compact_number(row.get('previous'))} → "
                f"{_compact_number(row.get('current'))}"
            )
        axis.legend(
            handles,
            labels,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=min(2, len(handles)),
            frameon=False,
            fontsize=8,
        )
        figure.subplots_adjust(left=0.09, right=0.98, top=0.9, bottom=0.27)
        return _save_figure(figure)


def _metrika_detail_table(doc, rows, *, first_header, metrics=("visits", "users", "bounce_rate")):
    metric_headers = {
        "visits": "Визиты",
        "users": "Посетители",
        "bounce_rate": "Отказы, %",
    }
    period_headers = []
    for code in metrics:
        period_headers.extend(
            (
                f"{metric_headers[code]}\nСегмент A",
                f"{metric_headers[code]}\nСегмент B",
            )
        )
    if len(metrics) == 3:
        widths = [5.3, *([2.2] * 6)]
    elif len(metrics) == 2:
        widths = [8.5, *([2.5] * 4)]
    else:
        widths = [10.5, 4.0, 4.0]
    table_rows = []
    for label, current, previous in rows:
        values = [label]
        for code in metrics:
            values.extend(
                (
                    _provider_value(code, previous.get(code)),
                    _provider_value(code, current.get(code)),
                )
            )
        table_rows.append(tuple(values))
    table = _table(
        doc,
        (first_header, *period_headers),
        table_rows,
        widths,
        header_fill="F7F7F7",
    )
    for cell in table.rows[0].cells[1:]:
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in cell.paragraphs[0].runs:
            run.font.size = Pt(7)
            run.font.color.rgb = RGBColor.from_string("7A8796")
    for row_index, (_label, current, previous) in enumerate(rows, start=1):
        for metric_index, code in enumerate(metrics):
            previous_cell = table.rows[row_index].cells[1 + metric_index * 2]
            current_cell = table.rows[row_index].cells[2 + metric_index * 2]
            previous_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            current_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            delta = _relative_delta(current.get(code), previous.get(code))
            if delta is None:
                continue
            change = current_cell.add_paragraph(_number(delta, "%", decimal_places=2))
            change.alignment = WD_ALIGN_PARAGRAPH.CENTER
            change.paragraph_format.space_after = Pt(0)
            change.runs[0].font.size = Pt(7)
            change.runs[0].font.color.rgb = RGBColor.from_string(
                _change_color(
                    current.get(code),
                    previous.get(code),
                    lower_is_better=code == "bounce_rate",
                )
            )
    _set_table_borders(table, "E2E4E8", size="3")
    return table


def _metrika_search_engine_text(current):
    total = sum((row.get("visits") or Decimal(0) for row in current.values()), Decimal(0))
    ordered = []
    for label in ("Яндекс", "Google"):
        values = current.get(label)
        if values:
            share = (
                _number(values["visits"] * 100 / total, "%", decimal_places=2) if total else None
            )
            ordered.append(
                f"{label} составляет {share} от всего поискового трафика"
                if share is not None
                else f"доля {label} не рассчитана"
            )
    other = total - sum(
        (current.get(label, {}).get("visits", Decimal(0)) for label in ("Яндекс", "Google")),
        Decimal(0),
    )
    suffix = (
        "На остальные поисковые системы приходится менее 1% всего поискового трафика, "
        "их данными можно пренебречь."
        if total and other * 100 / total < 1
        else (
            "На остальные поисковые системы приходится "
            f"{_number(other * 100 / total, '%', decimal_places=1)}."
        )
        if total
        else ""
    )
    return ". ".join(ordered) + (". " if ordered else "") + suffix


def _region_key(row):
    area = str(_row_dimension(row, 0).get("name") or "").casefold()
    city = str(_row_dimension(row, 1).get("name") or "").casefold()
    if city in {"москва", "moscow"}:
        return "moscow"
    if city in {"санкт-петербург", "saint petersburg", "st. petersburg"}:
        return "saint_petersburg"
    if "область не определена" in area or area in {"area not defined"}:
        return "area_undefined"
    if city in {"", "не определено", "undefined", "not defined"}:
        return "undefined"
    return city or area


REGION_LABELS = {
    "moscow": "Москва",
    "saint_petersburg": "Санкт-Петербург",
    "undefined": "Не определено",
    "area_undefined": "Область не определена",
}


def _landing_url(row):
    return str(_row_dimension(row, 1).get("name") or _row_dimension(row, 1).get("id") or "")


def _url_group(payload, url):
    for group in sorted(
        (item for item in payload.get("project", {}).get("url_groups", []) if item.get("active")),
        key=lambda item: (-int(item.get("priority") or 0), item.get("name") or ""),
    ):
        for rule in sorted(
            (item for item in group.get("rules", []) if item.get("active")),
            key=lambda item: -int(item.get("priority") or 0),
        ):
            pattern = str(rule.get("pattern") or "")
            kind = rule.get("type")
            matched = (
                url.startswith(pattern)
                if kind == "starts_with"
                else pattern in url
                if kind == "contains"
                else re.search(pattern, url) is not None
                if kind == "regex"
                else False
            )
            if matched:
                return group.get("name") or group.get("slug")
    return "Остальные страницы"


def _metrika_goals_chart(periods):
    if not periods:
        return None
    labels = [_month_short(row["period_start"]) for row in periods]
    conversion = [float(row.get("conversion_rate") or 0) for row in periods]
    visits = [float(row.get("visits") or 0) for row in periods]
    reaches = [float(row.get("reaches") or 0) for row in periods]
    with plt.rc_context({"font.family": CHART_FONT, "font.size": 9}):
        figure, left = plt.subplots(figsize=(5.35, 1.7), dpi=150, facecolor="white")
        right = left.twinx()
        left.plot(labels, conversion, color="#7A45E5", linewidth=1.5)
        right.plot(labels, visits, color="#FF3399", linewidth=1.5)
        right.plot(labels, reaches, color="#0FBDA0", linewidth=1.5)
        _style_axis(left)
        right.grid(False)
        right.tick_params(colors="#8B98A7", labelsize=7, length=0)
        for spine in right.spines.values():
            spine.set_visible(False)
        left.tick_params(labelsize=7)
        figure.subplots_adjust(left=0.1, right=0.9, top=0.94, bottom=0.25)
        return _save_figure(figure)


def _style_metrika_url_column(table):
    for row in table.rows[1:]:
        cell = row.cells[0]
        for run in cell.paragraphs[0].runs:
            run.font.color.rgb = RGBColor.from_string("5277D5")
            run.font.size = Pt(7.5)
        depth = min(4, len([part for part in urlsplit(cell.text).path.split("/") if part]))
        cell.paragraphs[0].paragraph_format.left_indent = Cm(depth * 0.16)


def _landing_pages_table(doc, current, previous, *, limit=20):
    ordered = sorted(current.items(), key=lambda item: item[1]["visits"], reverse=True)[:limit]
    table = _metrika_detail_table(
        doc,
        [(url, values, previous.get(url, {})) for url, values in ordered],
        first_header="Страница входа",
        metrics=("visits", "users"),
    )
    _style_metrika_url_column(table)
    return table


def _landing_comparison_table(doc, current_rows, previous_rows, engine):
    current = _aggregate_detail_rows(
        [row for row in current_rows if _search_engine_name(row) == engine], _landing_url
    )
    previous = _aggregate_detail_rows(
        [row for row in previous_rows if _search_engine_name(row) == engine], _landing_url
    )
    ordered = sorted(current.items(), key=lambda item: item[1]["visits"], reverse=True)[:20]
    table = _metrika_detail_table(
        doc,
        [(url, values, previous.get(url, {})) for url, values in ordered],
        first_header="Страница входа",
        metrics=("visits", "users"),
    )
    _style_metrika_url_column(table)
    return table


def _goal_card(doc, goal, periods):
    table = doc.add_table(rows=2, cols=2)
    table.autofit = False
    table.style = "Report Table"
    _set_cell_width(table.rows[0].cells[0], 5.2)
    _set_cell_width(table.rows[0].cells[1], 13.3)
    heading = table.rows[0].cells[0].merge(table.rows[0].cells[1])
    heading.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    heading.text = ""
    title = heading.paragraphs[0]
    title.paragraph_format.space_after = Pt(1)
    title_run = title.add_run(_clean(goal.get("label") or goal.get("name") or "Цель"))
    title_run.bold = True
    title_run.font.size = Pt(10)
    details = heading.add_paragraph()
    details.paragraph_format.space_after = Pt(0)
    detail_text = f"ID {_clean(goal.get('goal_id'))}"
    identifier = goal.get("identifier") or goal.get("condition")
    if identifier:
        detail_text += f"    идентификатор: {_clean(identifier)}"
    detail_run = details.add_run(detail_text)
    detail_run.font.size = Pt(7.5)
    detail_run.font.color.rgb = RGBColor.from_string("667180")

    metrics_cell, chart_cell = table.rows[1].cells
    metrics_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    chart_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _set_cell_width(metrics_cell, 5.2)
    _set_cell_width(chart_cell, 13.3)
    metrics_cell.text = ""
    metrics = (
        ("7A45E5", "Конверсия", _number(goal.get("conversion_rate"), "%", decimal_places=2)),
        ("FF3399", "Целевые визиты", _number(goal.get("visits"), decimal_places=0)),
        ("0FBDA0", "Достижения цели", _number(goal.get("reaches"), decimal_places=0)),
    )
    for index, (color, label, value) in enumerate(metrics):
        paragraph = metrics_cell.paragraphs[0] if index == 0 else metrics_cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(1)
        marker = paragraph.add_run("■ ")
        marker.font.color.rgb = RGBColor.from_string(color)
        marker.font.size = Pt(8)
        label_run = paragraph.add_run(f"{label}  ")
        label_run.font.size = Pt(8)
        value_run = paragraph.add_run(value)
        value_run.font.size = Pt(8)
        value_run.bold = True
    chart_cell.text = ""
    chart = _metrika_goals_chart(periods)
    if chart:
        chart_paragraph = chart_cell.paragraphs[0]
        chart_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        chart_paragraph.paragraph_format.space_after = Pt(0)
        chart_paragraph.add_run().add_picture(chart, width=Cm(12.7))
    _set_table_borders(table, "E2E7EF", size="3")
    for row in table.rows:
        _prevent_row_split(row)
    doc.add_paragraph(style="Compact")
    return table


def _render_metrika(doc, payload, blocks):
    traffic_enabled = section_enabled(payload, "traffic")
    sources_enabled = section_enabled(payload, "traffic_sources")
    geography_enabled = section_enabled(payload, "geography")
    options = payload.get("display_options", {})
    if not options.get("include_metrika", True):
        return
    doc.add_heading("3) Сводная информация по переходам на сайт (Яндекс.Метрика)", level=1)
    period_details = _period_details(payload, "yandex_metrika")
    if period_details:
        doc.add_paragraph(
            f"Период: {date.fromisoformat(str(period_details[0]['period_start'])[:10]):%d.%m.%Y} — "
            f"{date.fromisoformat(str(period_details[-1]['period_end'])[:10]):%d.%m.%Y}. "
            "Детализация: по месяцам.",
            style="Chart Caption",
        )
    if sources_enabled:
        doc.add_paragraph("Все источники", style="Table Heading")
        facts = _metric_source(payload, "yandex_metrika").get("traffic_source_dynamics", {})
        _add_report_picture(doc, _metrika_sources_chart(facts))
        if options.get("include_metrika_sources_table"):
            rows = [
                (
                    _metrika_source_label(name),
                    _number((fact.get("change") or {}).get("previous")),
                    _number((fact.get("change") or {}).get("current")),
                    _number(fact.get("share_percent"), "%", decimal_places=1),
                    _number(
                        (fact.get("change") or {}).get("relative_percent"),
                        "%",
                        decimal_places=1,
                    ),
                )
                for name, fact in sorted(facts.items())
            ]
            if rows:
                table = _table(
                    doc,
                    ("Источник", "Предыдущий период", "Текущий период", "Доля", "Изменение"),
                    rows,
                    [5.4, 3.3, 3.3, 3.0, 3.5],
                    header_fill="F7F7F7",
                )
                _set_table_borders(table, "E2E4E8", size="3")
    if traffic_enabled:
        search_periods = _metrika_period_rows(payload, "search_engines")
        if section_enabled(payload, "metrika_search_engines") and search_periods:
            doc.add_paragraph("Поисковые системы", style="Table Heading")
            current = _aggregate_detail_rows(search_periods[-1]["rows"], _search_engine_name)
            previous = (
                _aggregate_detail_rows(search_periods[-2]["rows"], _search_engine_name)
                if len(search_periods) >= 2
                else {}
            )
            ordered = sorted(current, key=lambda key: current[key]["visits"], reverse=True)[:4]
            colors = {"Google": "#7A45E5", "Яндекс": "#FF3399"}
            chart_rows = [
                {
                    "label": label,
                    "previous": previous.get(label, {}).get("visits"),
                    "current": current[label].get("visits"),
                    "color": colors.get(label),
                }
                for label in ordered
            ]
            doc.add_paragraph(
                "Динамика по поисковым системам за квартал:",
                style="Chart Caption",
            )
            _add_report_picture(doc, _metrika_search_quarter_chart(search_periods))
            if len(search_periods) >= 2:
                comparison_start = date.fromisoformat(str(search_periods[-2]["period_start"])[:10])
                comparison_end = date.fromisoformat(str(search_periods[-1]["period_end"])[:10])
                doc.add_paragraph(
                    f"Период сравнения: {comparison_start:%d.%m.%Y} — "
                    f"{comparison_end:%d.%m.%Y}. Детализация: по месяцам.",
                    style="Chart Caption",
                )
            _add_report_picture(
                doc,
                _metrika_comparison_bars(chart_rows, title="Визиты из поисковых систем"),
            )
            _metrika_detail_table(
                doc,
                [(label, current[label], previous.get(label, {})) for label in ordered],
                first_header="Поисковая система",
            )
            movement = []
            for label in ("Яндекс", "Google"):
                if label not in current:
                    continue
                delta = _relative_delta(
                    current[label].get("visits"), previous.get(label, {}).get("visits")
                )
                if delta is not None:
                    movement.append(
                        f"Трафик из {label} {'увеличился' if delta >= 0 else 'снизился'} "
                        f"на {_number(abs(delta), '%', decimal_places=1)}"
                    )
            total_current = sum((row["visits"] for row in current.values()), Decimal(0))
            total_previous = sum((row["visits"] for row in previous.values()), Decimal(0))
            total_delta = _relative_delta(total_current, total_previous)
            if total_delta is not None:
                movement.append(
                    f"Общий поисковый трафик {'увеличился' if total_delta >= 0 else 'снизился'} "
                    f"на {_number(abs(total_delta), '%', decimal_places=1)}"
                )
            doc.add_paragraph(
                ". ".join(movement)
                + (". " if movement else "")
                + _metrika_search_engine_text(current)
            )
    if geography_enabled:
        geography_periods = _metrika_period_rows(payload, "search_geography")
        current = (
            _aggregate_detail_rows(geography_periods[-1]["rows"], _region_key)
            if geography_periods
            else {}
        )
        previous = (
            _aggregate_detail_rows(geography_periods[-2]["rows"], _region_key)
            if len(geography_periods) >= 2
            else {}
        )
        flags = {
            "moscow": "geography_moscow",
            "saint_petersburg": "geography_saint_petersburg",
            "undefined": "geography_undefined",
            "area_undefined": "geography_area_undefined",
        }
        selected = [
            key for key, flag in flags.items() if options.get(flag, True) and key in current
        ]
        doc.add_paragraph(
            "Сравнение трафика за два последних месяца по основным регионам:",
            style="Table Heading",
        )
        if len(geography_periods) >= 2:
            comparison_start = date.fromisoformat(str(geography_periods[-2]["period_start"])[:10])
            comparison_end = date.fromisoformat(str(geography_periods[-1]["period_end"])[:10])
            doc.add_paragraph(
                f"Период сравнения: {comparison_start:%d.%m.%Y} — "
                f"{comparison_end:%d.%m.%Y}. Детализация: по месяцам.",
                style="Chart Caption",
            )
        _add_report_picture(
            doc,
            _metrika_comparison_bars(
                [
                    {
                        "label": REGION_LABELS[key],
                        "previous": previous.get(key, {}).get("visits"),
                        "current": current[key].get("visits"),
                        "color": METRIKA_COLORS[index % len(METRIKA_COLORS)],
                    }
                    for index, key in enumerate(selected)
                ],
                title="Поисковый трафик по регионам",
            ),
        )
        if selected:
            _metrika_detail_table(
                doc,
                [(REGION_LABELS[key], current[key], previous.get(key, {})) for key in selected],
                first_header="Регион",
            )
            total_current = sum((row["visits"] for row in current.values()), Decimal(0))
            total_previous = sum((row["visits"] for row in previous.values()), Decimal(0))
            for key in selected:
                if key in {"undefined", "area_undefined"}:
                    continue
                delta = _relative_delta(
                    current[key].get("visits"), previous.get(key, {}).get("visits")
                )
                current_share = (
                    current[key]["visits"] * 100 / total_current if total_current else None
                )
                previous_share = (
                    previous.get(key, {}).get("visits", 0) * 100 / total_previous
                    if total_previous
                    else None
                )
                doc.add_paragraph(
                    f"Трафик из региона «{REGION_LABELS[key]}» "
                    f"{'увеличился' if delta is not None and delta >= 0 else 'снизился'} на "
                    f"{_number(abs(delta), '%', decimal_places=1) if delta is not None else '—'}. "
                    f"Изменение доли трафика: предыдущий месяц — "
                    f"{_number(previous_share, '%', decimal_places=1)}, отчётный месяц — "
                    f"{_number(current_share, '%', decimal_places=1)}."
                )

    landing_periods = _metrika_period_rows(payload, "landing_pages")
    if landing_periods:
        current_rows = landing_periods[-1]["rows"]
        previous_rows = landing_periods[-2]["rows"] if len(landing_periods) >= 2 else []
        current_pages = _aggregate_detail_rows(current_rows, _landing_url)
        previous_pages = _aggregate_detail_rows(previous_rows, _landing_url)
        if section_enabled(payload, "metrika_landing_pages"):
            doc.add_paragraph("Популярные страницы входа", style="Table Heading")
            doc.add_paragraph(
                "Ниже приведены значения количества переходов в отчётном месяце по сравнению "
                "с предыдущим только по поисковому трафику по страницам."
            )
            _landing_pages_table(doc, current_pages, previous_pages)
            total = sum((row["visits"] for row in current_pages.values()), Decimal(0))
            root = next(
                (
                    (url, values)
                    for url, values in current_pages.items()
                    if urlsplit(url).path in {"", "/"}
                ),
                None,
            )
            internal = [
                (url, values)
                for url, values in current_pages.items()
                if urlsplit(url).path not in {"", "/"}
            ]
            popular = max(internal, key=lambda item: item[1]["visits"], default=None)
            text = []
            if root and total:
                text.append(
                    f"На главную страницу приходится "
                    f"{_number(root[1]['visits'] * 100 / total, '%', decimal_places=2)} всех "
                    f"визитов ({_number(root[1]['visits'], decimal_places=0)} визитов за месяц)"
                )
            if popular and total:
                text.append(
                    f"Самой популярной внутренней страницей является {popular[0]} "
                    f"({_number(popular[1]['visits'], decimal_places=0)} визитов — "
                    f"{_number(popular[1]['visits'] * 100 / total, '%', decimal_places=2)})"
                )
            if text:
                doc.add_paragraph(". ".join(text) + ".")
        if section_enabled(payload, "metrika_landing_page_comparison"):
            for engine in ("Яндекс", "Google"):
                doc.add_paragraph(
                    f"Страницы входа в сравнении двух месяцев для трафика из {engine}:",
                    style="Table Heading",
                )
                _landing_comparison_table(doc, current_rows, previous_rows, engine)
        group_codes = (
            ("metrika_url_groups", "Сравнение информационных и коммерческих страниц"),
            ("metrika_sections", "Данные по разделам"),
            ("metrika_categories", "Основные прорабатываемые категории"),
        )
        current_groups = _aggregate_detail_rows(
            current_rows, lambda row: _url_group(payload, _landing_url(row))
        )
        previous_groups = _aggregate_detail_rows(
            previous_rows, lambda row: _url_group(payload, _landing_url(row))
        )
        for code, title in group_codes:
            if not section_enabled(payload, code):
                continue
            doc.add_paragraph(title, style="Table Heading")
            if code == "metrika_url_groups":
                ordered_pages = sorted(
                    current_pages,
                    key=lambda key: current_pages[key]["visits"],
                    reverse=True,
                )[:20]
                table = _metrika_detail_table(
                    doc,
                    [
                        (url, current_pages[url], previous_pages.get(url, {}))
                        for url in ordered_pages
                    ],
                    first_header="Страница входа",
                )
                _style_metrika_url_column(table)
                continue
            ordered_groups = sorted(
                current_groups, key=lambda key: current_groups[key]["visits"], reverse=True
            )
            _metrika_detail_table(
                doc,
                [
                    (label, current_groups[label], previous_groups.get(label, {}))
                    for label in ordered_groups
                ],
                first_header="Раздел",
            )
            for label in ordered_groups:
                delta = _relative_delta(
                    current_groups[label]["visits"],
                    previous_groups.get(label, {}).get("visits"),
                )
                doc.add_paragraph(
                    f"Трафик на раздел «{label}» "
                    f"{'увеличился' if delta is not None and delta >= 0 else 'снизился'} на "
                    f"{_number(abs(delta), '%', decimal_places=1) if delta is not None else '—'}."
                )

    if section_enabled(payload, "metrika_goals") and period_details:
        robotness = options.get("metrika_robotness", "humans")
        goal_periods = []
        for detail in period_details:
            source_payload = _detail_payload(detail)
            goal_periods.append(
                {
                    "period_start": detail.get("period_start"),
                    "rows": (source_payload.get("goals_by_robotness") or {}).get(robotness)
                    or source_payload.get("goals")
                    or [],
                }
            )
        current_goals = goal_periods[-1]["rows"]
        if current_goals:
            doc.add_paragraph("Сводная информация по конверсии", style="Table Heading")
            first_date = date.fromisoformat(str(period_details[0]["period_start"])[:10])
            last_date = date.fromisoformat(str(period_details[-1]["period_end"])[:10])
            robotness_label = "только люди" if robotness == "humans" else "все визиты"
            doc.add_paragraph(
                f"Период: {first_date:%d.%m.%Y} — {last_date:%d.%m.%Y} · "
                f"Детализация: по месяцам · Сегмент: переходы из поисковых систем · "
                f"Роботность: {robotness_label}",
                style="Chart Caption",
            )
            for goal in current_goals:
                goal_id = str(goal.get("goal_id"))
                series = []
                for period in goal_periods:
                    row = next(
                        (item for item in period["rows"] if str(item.get("goal_id")) == goal_id),
                        None,
                    )
                    if row:
                        series.append({"period_start": period["period_start"], **row})
                _goal_card(doc, goal, series)


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


def _render_work(doc, payload, narrative):
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    works = payload.get("completed_work", [])
    if works:
        for work in works:
            paragraph = doc.add_paragraph(style="List Number")
            title = work.get("title") or work.get("category") or "Работа"
            paragraph.add_run(_clean(title)).bold = True
            comment_lines = [
                re.sub(r"^[\s•●\-–—\d.)]+", "", line).strip()
                for line in str(work.get("comment") or "").splitlines()
                if line.strip()
            ]
            for line in comment_lines:
                doc.add_paragraph(_clean(line), style="List Bullet 2")
            if show_urls:
                for label, value in (
                    ("Страница", work.get("page_or_material_name") or work.get("url")),
                    ("Результат", work.get("result_url")),
                ):
                    if value:
                        doc.add_paragraph(f"{label}: {_clean(value)}", style="Compact")
    else:
        doc.add_paragraph(
            _clean(narrative) or "Выполненные работы отсутствуют.", style="Data Missing"
        )


def _configure_document(doc, domain, period):
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = Cm(0.75)
    section.bottom_margin = Cm(0.5)
    section.left_margin = Cm(1.5)
    section.right_margin = Cm(1.0)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[name]
        style.font.name = "Arial"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
        style.font.color.rgb = RGBColor.from_string("000000")
    doc.styles["Normal"].font.size = Pt(11)
    for name, size in (("Heading 1", 12), ("Heading 2", 11), ("Heading 3", 11)):
        doc.styles[name].font.size = Pt(size)
        doc.styles[name].font.bold = True
        doc.styles[name].paragraph_format.keep_with_next = True
        doc.styles[name].paragraph_format.space_after = Pt(6)
    for style_name, size, italic in (
        ("Chart Caption", 9, True),
        ("Data Missing", 10, True),
        ("KPI", 11, False),
        ("Depth Note", 9, True),
        ("Compact", 10, False),
        ("Table Heading", 10, False),
        ("Table Comment", 10, False),
    ):
        style = doc.styles.add_style(style_name, 1)
        style.font.name = "Arial"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
        style.font.size = Pt(size)
        style.font.italic = italic
    doc.styles["Compact"].paragraph_format.space_after = Pt(0)
    doc.styles["Table Heading"].font.bold = True
    doc.styles["Table Heading"].paragraph_format.keep_with_next = True
    doc.styles["Table Comment"].paragraph_format.space_before = Pt(4)
    table_style = doc.styles.add_style("Report Table", 3)
    table_style.base_style = doc.styles["Table Grid"]
    table_style.font.name = "Arial"
    table_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
    table_style.font.size = Pt(8.5)


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
    narrative_rows = list(narratives)
    blocks = {block.section_code: block.effective_text for block in narrative_rows}
    table_comments = {
        block.section_code: block.edited_text.strip()
        for block in narrative_rows
        if block.edited_text.strip()
    }
    engine_order = {"yandex": 0, "google": 1}
    segments = sorted(
        payload.get("calculated", {}).get("positions", {}).get("segments", []),
        key=lambda item: (
            engine_order.get(item.get("search_engine"), 99),
            item.get("region") or "",
        ),
    )
    _render_topvisor(doc, payload, segments, table_comments)
    _render_webmaster(doc, payload, blocks)
    _render_metrika(doc, payload, blocks)
    warning_messages = {
        _clean(issue.message)
        for issue in issues
        if issue.severity == "warning"
        and section_enabled(payload, issue.section_code)
        and issue.section_code != "completed_work"
    }
    for message in sorted(warning_messages):
        doc.add_paragraph("Предупреждение: " + message, style="Depth Note")
    if section_enabled(payload, "completed_work"):
        doc.add_heading(TITLES["completed_work"], level=1)
        _render_work(
            doc,
            payload,
            blocks.get("completed_work") or "Выполненные работы отсутствуют.",
        )
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
        office_binary = shutil.which("libreoffice") or shutil.which("soffice")
        if office_binary is None:
            raise RuntimeError("LibreOffice is not installed")
        result = subprocess.run(
            [
                office_binary,
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
