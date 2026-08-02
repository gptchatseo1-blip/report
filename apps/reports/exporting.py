"""Deterministic, offline renderers for an immutable report version."""

import hashlib
import io
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import matplotlib
from django.core.files.base import ContentFile
from django.utils import timezone
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

from .models import GeneratedArtifact, NarrativeBlock, ReportDatasetSnapshot, ValidationIssue
from .narratives import SECTION_ORDER
from .validation import get_publication_readiness

GENERATOR_VERSION = "mvp1.0"
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


class ExportBlocked(Exception):
    pass


def _clean(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))


def _month(value):
    parsed = date.fromisoformat(str(value)[:10])
    return f"{MONTHS[parsed.month - 1]} {parsed.year}"


def _repeat_header(row):
    props = row._tr.get_or_add_trPr()
    element = OxmlElement("w:tblHeader")
    element.set(qn("w:val"), "true")
    props.append(element)


def _table(doc, headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for cell, value in zip(table.rows[0].cells, headers, strict=True):
        cell.text = _clean(value)
    _repeat_header(table.rows[0])
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values, strict=True):
            cell.text = _clean(value if value is not None else "—")
    return table


def _distribution_chart(segment):
    """Render only depth-confirmed ranges already present in snapshot calculations."""
    ranges = (segment.get("distribution") or {}).get("ranges") or {}
    if not ranges or not any(ranges.values()):
        return None
    with plt.rc_context({"font.family": "Carlito", "font.size": 9}):
        figure, axis = plt.subplots(figsize=(7.2, 3.2), dpi=120)
        axis.bar(list(ranges), list(ranges.values()), color="#315B7D")
        axis.set_ylabel("Количество запросов")
        axis.set_title(f"{segment.get('search_engine')} · {segment.get('region')}")
        figure.tight_layout()
        output = io.BytesIO()
        figure.savefig(output, format="png", dpi=120, metadata={"Software": GENERATOR_VERSION})
        plt.close(figure)
        output.seek(0)
        return output


def _docx(snapshot, narratives, issues, draft):
    payload = snapshot.payload
    project = payload.get("project", {})
    period = payload.get("periods", {}).get("report", {}).get("start")
    doc = Document()
    styles = doc.styles
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        styles[name].font.name = "Carlito"
        styles[name]._element.rPr.rFonts.set(qn("w:eastAsia"), "Carlito")
    styles["Normal"].font.size = Pt(11)
    if draft:
        p = doc.add_paragraph("ЧЕРНОВИК")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.runs[0].bold, p.runs[0].font.size = True, Pt(24)
    title = f"Отчёт по поисковому продвижению сайта {project.get('domain')} за {_month(period)}"
    doc.add_heading(_clean(title), 0)
    doc.add_paragraph(_clean(project.get("name")))
    doc.add_paragraph(f"Версия {snapshot.version.number}")
    doc.add_paragraph(f"Дата формирования: {timezone.localdate():%d.%m.%Y}")
    doc.add_page_break()
    footer = doc.sections[0].footer.paragraphs[0]
    footer.text = f"{_clean(project.get('domain'))} · {_month(period)} · "
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    blocks = {b.section_code: b.effective_text for b in narratives}
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    for code in SECTION_ORDER:
        doc.add_heading(TITLES[code], level=1)
        if code == "position_distribution":
            rows = []
            for segment in segments:
                dist = segment.get("distribution") or {}
                total = dist.get("total") or 0
                for label, count in (dist.get("ranges") or {}).items():
                    rows.append(
                        (
                            segment.get("search_engine"),
                            segment.get("region"),
                            segment.get("ranking_depth"),
                            label,
                            count,
                            round(count * 100 / total, 2) if total else 0,
                        )
                    )
            if rows:
                _table(
                    doc,
                    ("Поисковая система", "Регион", "Глубина", "Диапазон", "Количество", "Доля, %"),
                    rows,
                )
            for segment in segments:
                chart = _distribution_chart(segment)
                if chart:
                    doc.add_picture(chart, width=Cm(16))
        elif code == "top_11_20":
            rows = [
                (
                    r.get("query"),
                    r.get("frequency"),
                    r.get("position"),
                    r.get("group"),
                    r.get("target_url"),
                )
                for s in segments
                for r in s.get("top_11_20", [])
            ]
            if rows:
                _table(doc, ("Запрос", "Частотность", "Позиция", "Группа", "Релевантный URL"), rows)
        elif code == "completed_work":
            section = doc.add_section()
            section.orientation = WD_ORIENT.LANDSCAPE
            section.page_width, section.page_height = section.page_height, section.page_width
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
                )
            portrait = doc.add_section()
            portrait.orientation = WD_ORIENT.PORTRAIT
            portrait.page_width, portrait.page_height = Cm(21), Cm(29.7)
        text = blocks.get(code) or "Данные раздела отсутствуют."
        doc.add_paragraph(_clean(text))
        for issue in issues:
            if issue.severity == "warning" and issue.section_code == code:
                doc.add_paragraph("Предупреждение: " + _clean(issue.message))
    doc.add_heading("Происхождение данных", level=1)
    for source in payload.get("ranking_sources", []) + payload.get("source_snapshots", []):
        doc.add_paragraph(
            _clean(
                f"{source.get('source') or source.get('search_engine')} · "
                f"{source.get('date') or source.get('period_start')} · "
                f"{(source.get('provenance') or {}).get('method')}"
            )
        )
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def _xlsx(snapshot, draft):
    p = snapshot.payload
    wb = Workbook()
    ws = wb.active
    ws.title = "Метаданные"
    project = p.get("project", {})
    period = p.get("periods", {}).get("report", {}).get("start")
    meta = [
        ("проект", project.get("name")),
        ("домен", project.get("domain")),
        ("месяц", date.fromisoformat(str(period)[:10])),
        ("версия", snapshot.version.number),
        ("checksum snapshot", snapshot.checksum),
        ("дата создания", snapshot.created_at.replace(tzinfo=None)),
        ("formula_version", snapshot.formula_version),
        ("черновик", draft),
    ]
    for row in meta:
        ws.append(row)
    positions = wb.create_sheet("Позиции")
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
            "URL",
            "Фактическая глубина",
            "Источник",
        )
    )
    for source in p.get("ranking_sources", []):
        for row in source.get("positions", []):
            positions.append(
                (
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
                    (source.get("provenance") or {}).get("method"),
                )
            )
    history = wb.create_sheet("История")
    history.append(("Система", "Регион", "Месяц", "Глубина", "Распределение"))
    for s in p.get("calculated", {}).get("positions", {}).get("segments", []):
        for row in s.get("three_month_series", []):
            history.append(
                (
                    s.get("search_engine"),
                    s.get("region"),
                    date.fromisoformat(row["month"]),
                    row.get("ranking_depth"),
                    str((row.get("distribution") or {}).get("ranges", {})),
                )
            )
    metrics = wb.create_sheet("Метрика и Вебмастер")
    metrics.append(
        ("Источник", "Начало", "Конец", "Показатель", "Значение", "Единица", "Provenance")
    )
    for source in p.get("source_snapshots", []):
        for m in source.get("metrics", []):
            metrics.append(
                (
                    source.get("source"),
                    date.fromisoformat(source["period_start"]),
                    date.fromisoformat(source["period_end"]),
                    m.get("code"),
                    float(m["value"]) if m.get("value") is not None else None,
                    m.get("unit"),
                    (source.get("provenance") or {}).get("method"),
                )
            )
    work = wb.create_sheet("Выполненные работы")
    headers = (
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
    work.append(headers)
    for w in p.get("completed_work", []):
        work.append(
            (
                date.fromisoformat(w["date"]),
                w.get("category"),
                w.get("title"),
                w.get("status"),
                w.get("page_or_material_name") or w.get("url"),
                w.get("character_count"),
                w.get("responsible"),
                w.get("comment"),
                w.get("result_url"),
            )
        )
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        if sheet.max_column:
            sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="315B7D")
        for column in sheet.columns:
            letter = column[0].column_letter
            sheet.column_dimensions[letter].width = min(
                55, max(12, max(len(str(c.value or "")) for c in column) + 2)
            )
            for cell in column:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def _pdf(docx_bytes):
    with tempfile.TemporaryDirectory(prefix="seo-export-") as tmp:
        root = Path(tmp)
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
            NarrativeBlock.objects.filter(report_version=version).order_by("sort_order")
        )
        issues = list(ValidationIssue.objects.filter(version=version))
        log = "; ".join(_clean(i.message) for i in issues if i.severity == "warning")
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
        artifact.save()
        return artifact
    except Exception as exc:
        artifact.status = GeneratedArtifact.Status.FAILED
        artifact.generation_log = _clean(str(exc))[:2000]
        artifact.save(update_fields=["status", "generation_log"])
        raise
