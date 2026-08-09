"""Offline renderers whose business data comes only from a frozen report snapshot."""

import hashlib
import io
import re
import subprocess
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib
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
from .narratives import SECTION_ORDER
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
    "top_10": "TOP-10",
    "top_11_20": "TOP-11–20",
    "position_dynamics": "Динамика позиций",
    "traffic": "Трафик",
    "traffic_sources": "Источники трафика",
    "clicks_impressions": "Клики и показы",
    "ctr": "CTR",
    "indexing": "Индексация",
    "iks": "ИКС",
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
SOURCE_LABELS = {
    "yandex_metrika": "Яндекс Метрика",
    "yandex_webmaster": "Яндекс Вебмастер",
}
METRIC_LABELS = {
    "visits": "Визиты",
    "users": "Пользователи",
    "search_clicks": "Клики",
    "search_impressions": "Показы",
    "search_ctr": "CTR",
    "indexed_pages": "Индексируемые страницы",
    "iks": "ИКС",
    "quality_index": "ИКС",
}
PALETTE = ("#315B7D", "#E68A2E", "#4C956C", "#8D6A9F", "#D1495B", "#6C757D")


class ExportBlocked(Exception):
    pass


def _clean(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))


def _excel_safe(value):
    """Prevent spreadsheet programs from interpreting untrusted strings as formulas."""
    value = _clean(value)
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


def _number(value, suffix=""):
    if value is None:
        return "Данные недоступны"
    try:
        rendered = format(Decimal(str(value)).normalize(), "f").replace(".", ",")
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


def _table(doc, headers, rows, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Report Table"
    table.autofit = False
    widths = widths or [16 / len(headers)] * len(headers)
    for cell, value, width in zip(table.rows[0].cells, headers, widths, strict=True):
        cell.text = _clean(value)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _set_cell_width(cell, width)
    _repeat_header(table.rows[0])
    _prevent_row_split(table.rows[0])
    for values in rows:
        cells = table.add_row().cells
        _prevent_row_split(table.rows[-1])
        for cell, value, width in zip(cells, values, widths, strict=True):
            cell.text = _clean(value if value is not None and value != "" else "—")
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
            _set_cell_width(cell, width)
    return table


def _chart(series, *, title, ylabel, kind="line"):
    """Return a stable PNG, or None when no series has at least one known value."""
    useful = [(label, points) for label, points in series if any(v is not None for _, v in points)]
    if not useful:
        return None
    with plt.rc_context({"font.family": "Carlito", "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.4), dpi=120)
        for index, (label, points) in enumerate(useful):
            labels = [_month_short(month) for month, _ in points]
            values = [float(value) if value is not None else float("nan") for _, value in points]
            color = PALETTE[index % len(PALETTE)]
            if kind == "bar":
                offsets = [
                    (i - (len(useful) - 1) / 2) * 0.75 / len(useful) for i in range(len(useful))
                ]
                x = [position + offsets[index] for position in range(len(labels))]
                axis.bar(x, values, width=0.75 / len(useful), color=color, label=label)
                axis.set_xticks(range(len(labels)), labels)
            else:
                axis.plot(labels, values, marker="o", linewidth=2, color=color, label=label)
        axis.set_title(title)
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", alpha=0.2)
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


def _chart_narrative(text):
    sentences = re.split(r"(?<=[.!?])\s+", _clean(text))
    filtered = [
        sentence
        for sentence in sentences
        if "Проверка позиций в Google" not in sentence
        and "Глубина проверки позиций в Google" not in sentence
    ]
    return " ".join(filtered).strip() or "Данные представлены на графике."


def _segment_title(segment):
    engine = ENGINE_LABELS.get(
        segment.get("search_engine"), segment.get("search_engine") or "Поиск"
    )
    return f"{engine} · {segment.get('region') or 'регион не указан'}"


def _metric_source(payload, source_name):
    return payload.get("calculated", {}).get("sources", {}).get("sources", {}).get(source_name, {})


def _provenance_rows(payload, code, segment=None):
    if code in {"visibility", "position_distribution", "top_10", "top_11_20", "position_dynamics"}:
        sources = payload.get("ranking_sources", [])
        if segment:
            sources = [
                s
                for s in sources
                if s.get("search_engine") == segment.get("search_engine")
                and s.get("region") == segment.get("region")
            ]
    elif code in {"traffic", "traffic_sources"}:
        sources = [
            s for s in payload.get("source_snapshots", []) if s.get("source") == "yandex_metrika"
        ]
    elif code in {"clicks_impressions", "ctr", "indexing", "iks"}:
        sources = [
            s for s in payload.get("source_snapshots", []) if s.get("source") == "yandex_webmaster"
        ]
    else:
        sources = payload.get("completed_work", [])
    rows = []
    for source in sources:
        provenance = source.get("provenance") or {}
        rows.append(
            (
                SOURCE_LABELS.get(
                    source.get("source"),
                    ENGINE_LABELS.get(
                        source.get("search_engine"), source.get("search_engine") or "Журнал работ"
                    ),
                ),
                provenance.get("method") or "worklog",
                source.get("date")
                or (
                    f"{source.get('period_start')} — {source.get('period_end')}"
                    if source.get("period_start")
                    else source.get("date")
                ),
                provenance.get("retrieved_at")
                or provenance.get("generated_at")
                or provenance.get("updated_at"),
                provenance.get("response_checksum")
                or provenance.get("checksum")
                or provenance.get("import_batch_id")
                or source.get("id")
                or provenance.get("id"),
            )
        )
    return rows


def _add_provenance(doc, payload, code, segment=None):
    rows = _provenance_rows(payload, code, segment)
    doc.add_paragraph("Источник данных", style="Heading 3")
    if not rows:
        doc.add_paragraph("Сведения об источнике недоступны.", style="Data Missing")
        return
    _table(
        doc,
        ("Система", "Метод", "Период", "Дата получения", "Checksum / идентификатор"),
        rows,
        [2.7, 2.5, 3.3, 3.3, 4.2],
    )


def _current_position_source(payload, segment):
    report_month = str(payload.get("periods", {}).get("report", {}).get("start"))[:7]
    candidates = [
        s
        for s in payload.get("ranking_sources", [])
        if s.get("search_engine") == segment.get("search_engine")
        and s.get("region") == segment.get("region")
        and str(s.get("date"))[:7] == report_month
    ]
    return max(
        candidates, key=lambda item: (item.get("date") or "", item.get("id") or ""), default=None
    )


def _position_rows(source, start=None, end=None):
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
        rows.append(
            (
                item.get("query"),
                item.get("frequency"),
                position,
                item.get("group"),
                item.get("target_url"),
            )
        )
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
    for segment in segments:
        doc.add_heading(_segment_title(segment), level=2)
        points = [
            (row.get("month"), row.get("visibility"))
            for row in segment.get("three_month_series", [])
        ]
        _add_chart(
            doc,
            _chart(
                [("Видимость, %", points)],
                title=f"Динамика видимости · {_segment_title(segment)}",
                ylabel="%",
            ),
            _chart_narrative(narrative),
        )
        current = points[-1][1] if points else None
        doc.add_paragraph(f"Текущая видимость: {_number(current, '%')}", style="KPI")
        _table(
            doc,
            ("Период", "Видимость, %"),
            [(_month(month), _number(value)) for month, value in points],
            [8, 8],
        )
        _add_provenance(doc, payload, "visibility", segment)


def _render_distribution(doc, payload, segments, narrative):
    if not segments:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
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
        )
        _add_chart(doc, chart, _chart_narrative(narrative))
        _table(
            doc,
            ("Диапазон", "Количество", "Доля, %"),
            [(name, ranges[name], _number(share)) for name, share in current],
            [6, 5, 5],
        )
        _add_provenance(doc, payload, "position_distribution", segment)


def _render_top(doc, payload, segments, narrative, start, end, code):
    any_segment = False
    for segment in segments:
        source = _current_position_source(payload, segment)
        rows = _position_rows(source, start, end)
        if (
            code == "top_11_20"
            and not rows
            and payload.get("project", {}).get("top_11_20_mode") != "enabled"
        ):
            continue
        any_segment = True
        doc.add_heading(_segment_title(segment), level=2)
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
                ),
                _chart_narrative(narrative),
            )
            distribution = segment.get("distribution") or {}
            kpi = f"TOP-10: {_number(distribution.get('top_10'))}"
            if (segment.get("ranking_depth") or 0) >= 30:
                kpi += f" · TOP-30: {_number(distribution.get('top_30'))}"
            doc.add_paragraph(kpi, style="KPI")
        if rows:
            _table(
                doc,
                ("Запрос", "Частотность", "Позиция", "Группа", "Релевантный URL"),
                rows,
                [5.0, 2.3, 2.0, 3.0, 4.0],
            )
        else:
            doc.add_paragraph("Запросы в диапазоне отсутствуют.", style="Data Missing")
        doc.add_paragraph(_clean(narrative))
        _add_provenance(doc, payload, code, segment)
    return any_segment


def _render_position_dynamics(doc, payload, segments, narrative):
    if not segments:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
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
            ),
            _chart_narrative(narrative),
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
        all_rows = _position_rows(source)
        _start_landscape(doc)
        doc.add_heading("Все запросы отчётного периода", level=3)
        if all_rows:
            _table(
                doc,
                ("Запрос", "Частотность", "Позиция", "Группа", "Релевантный URL"),
                all_rows,
                [7.0, 2.7, 2.2, 5.0, 9.0],
            )
        else:
            doc.add_paragraph("Данные недоступны.", style="Data Missing")
        _return_to_portrait(doc)
        _add_provenance(doc, payload, "position_dynamics", segment)
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
        "traffic": ("yandex_metrika", ("visits", "users"), "Визиты и пользователи", "Количество"),
        "clicks_impressions": (
            "yandex_webmaster",
            ("search_impressions", "search_clicks"),
            "Показы и клики",
            "Количество",
        ),
        "ctr": ("yandex_webmaster", ("search_ctr",), "CTR", "%"),
        "indexing": ("yandex_webmaster", ("indexed_pages",), "Индексируемые страницы", "Страницы"),
        "iks": ("yandex_webmaster", ("iks", "quality_index"), "ИКС", "Значение"),
    }
    source, codes, title, ylabel = configs[code]
    series = _metric_series(payload, source, codes)
    if code == "iks" and not any(points for _, points in series[:1]):
        series = series[1:]
        codes = ("quality_index",)
    _add_chart(doc, _chart(series, title=title, ylabel=ylabel), narrative)
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
    )
    _add_provenance(doc, payload, code)


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
        doc, _chart(series, title="Структура источников трафика", ylabel="Визиты"), narrative
    )
    rows = []
    for name, fact in facts.items():
        change = fact.get("change") or {}
        rows.append(
            (
                name,
                _number(change.get("current")),
                _number(fact.get("share_percent"), "%"),
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
        )
    else:
        doc.add_paragraph("Данные недоступны.", style="Data Missing")
    _add_provenance(doc, payload, "traffic_sources")


def _render_work(doc, payload, narrative):
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
        _start_landscape(doc)
        doc.add_heading("Таблица выполненных работ", level=3)
        _table(
            doc,
            (
                "Дата",
                "Категория",
                "Название",
                "Статус",
                "Страница или материал",
                "Объём",
                "Ответственный",
                "Комментарий",
                "Результат",
            ),
            rows,
            [2.2, 3.0, 4.5, 2.5, 4.2, 2.0, 3.2, 4.2, 4.2],
        )
        _return_to_portrait(doc)
    else:
        doc.add_paragraph("Выполненные работы отсутствуют.", style="Data Missing")
    doc.add_paragraph(_clean(narrative))
    _add_provenance(doc, payload, "completed_work")


def _configure_document(doc, domain, period, version_number):
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
    footer.text = f"{_clean(domain)} · {_month(period)} · версия {version_number} · стр. "
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)


def _docx(snapshot, narratives, issues, draft):
    payload = snapshot.payload
    project = payload.get("project", {})
    period = payload.get("periods", {}).get("report", {}).get("start")
    doc = Document()
    _configure_document(doc, project.get("domain"), period, snapshot.version.number)
    if draft:
        paragraph = doc.add_paragraph("ЧЕРНОВИК")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.runs[0].bold = True
        paragraph.runs[0].font.size = Pt(26)
        paragraph.runs[0].font.color.rgb = RGBColor(190, 35, 35)
    title = f"Отчёт по поисковому продвижению сайта {project.get('domain')} за {_month(period)}"
    doc.add_heading(_clean(title), 0)
    doc.add_paragraph(_clean(project.get("name")))
    doc.add_paragraph(f"Версия {snapshot.version.number}")
    doc.add_paragraph(f"Дата формирования: {timezone.localdate():%d.%m.%Y}")
    doc.add_page_break()
    blocks = {block.section_code: block.effective_text for block in narratives}
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    top_mode = project.get("top_11_20_mode", "auto")
    for code in SECTION_ORDER:
        if code == "top_11_20" and top_mode == "disabled":
            continue
        narrative = blocks.get(code) or "Данные раздела отсутствуют."
        if (
            code == "top_11_20"
            and top_mode == "auto"
            and not any(s.get("top_11_20") for s in segments)
        ):
            continue
        doc.add_heading(TITLES[code], level=1)
        if code == "visibility":
            _render_visibility(doc, payload, segments, narrative)
        elif code == "position_distribution":
            _render_distribution(doc, payload, segments, narrative)
        elif code == "top_10":
            _render_top(doc, payload, segments, narrative, 1, 10, code)
        elif code == "top_11_20":
            _render_top(doc, payload, segments, narrative, 11, 20, code)
        elif code == "position_dynamics":
            _render_position_dynamics(doc, payload, segments, narrative)
        elif code == "traffic_sources":
            _render_traffic_sources(doc, payload, narrative)
        elif code in {"traffic", "clicks_impressions", "ctr", "indexing", "iks"}:
            _render_metrics(doc, payload, code, narrative)
        elif code == "completed_work":
            _render_work(doc, payload, narrative)
        for issue in issues:
            if issue.severity == "warning" and issue.section_code == code:
                doc.add_paragraph("Предупреждение: " + _clean(issue.message), style="Depth Note")
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def _xlsx_value(value):
    return _excel_safe(value) if isinstance(value, str) else value


def _xlsx(snapshot, draft):
    payload = snapshot.payload
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
        ("дата создания", snapshot.created_at.replace(tzinfo=None)),
        ("formula_version", snapshot.formula_version),
        ("глубины", depths),
        ("черновик", draft),
    ):
        metadata.append(tuple(_xlsx_value(value) for value in row))
    positions = workbook.create_sheet("Позиции")
    positions.append(
        (
            "Поисковая система",
            "Регион",
            "Дата",
            "Запрос",
            "Частотность",
            "Позиция",
            "Статус",
            "Группа",
            "Релевантный URL",
            "Фактическая глубина",
            "Provenance",
        )
    )
    for source in payload.get("ranking_sources", []):
        provenance = (source.get("provenance") or {}).get("method") or (
            source.get("provenance") or {}
        ).get("import_batch_id")
        for row in source.get("positions", []):
            positions.append(
                tuple(
                    _xlsx_value(value)
                    for value in (
                        source.get("search_engine"),
                        source.get("region"),
                        date.fromisoformat(source["date"]),
                        row.get("query"),
                        row.get("frequency"),
                        row.get("position"),
                        row.get("status"),
                        row.get("group"),
                        row.get("target_url"),
                        source.get("ranking_depth"),
                        provenance,
                    )
                )
            )
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
    metrics.append(
        ("Источник", "Начало", "Конец", "Показатель", "Значение", "Единица", "Provenance")
    )
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
                        (source.get("provenance") or {}).get("method"),
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
                    f"traffic_source_{_excel_safe(name)}_normalized_per_day",
                    float(point["value"]) if point.get("value") is not None else None,
                    "count_per_day",
                    "calculated snapshot fact",
                )
            )
    work = workbook.create_sheet("Выполненные работы")
    work.append(
        (
            "Дата",
            "Категория",
            "Название",
            "Статус",
            "Страница или материал",
            "Объём",
            "Ответственный",
            "Комментарий",
            "Результат",
        )
    )
    for item in payload.get("completed_work", []):
        work.append(
            tuple(
                _xlsx_value(value)
                for value in (
                    date.fromisoformat(item["date"]),
                    item.get("category"),
                    item.get("title"),
                    item.get("status"),
                    item.get("page_or_material_name") or item.get("url"),
                    item.get("character_count"),
                    item.get("responsible"),
                    item.get("comment"),
                    item.get("result_url"),
                )
            )
        )
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
            timeout=120,
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
        return pdf.read_bytes(), _clean((result.stdout + " " + result.stderr)[:1000])


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
            data, conversion_log = _pdf(_docx(snapshot, narratives, issues, is_draft))
            log = "; ".join(filter(None, (log, conversion_log)))
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
        artifact.generation_log = _clean(str(exc))[:2000]
        artifact.save(update_fields=["status", "generation_log"])
        raise
