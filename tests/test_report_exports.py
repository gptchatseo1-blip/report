import io
import shutil
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from docx import Document
from docx.enum.section import WD_ORIENT
from openpyxl import load_workbook
from pypdf import PdfReader

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.metrics.synthetic import sync_synthetic_metrics
from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import Report, ReportDatasetSnapshot
from apps.reports.narratives import SECTION_ORDER
from apps.reports.services import create_report_version

pytestmark = pytest.mark.django_db


def _ranking(project, month, depth=100, count=36, engine="google", region="Россия"):
    snapshot = RankingSnapshot.objects.create(
        project=project,
        snapshot_date=month,
        search_engine=engine,
        region=region,
        ranking_depth=depth,
        depth_raw=f"TOP-{depth}",
        visibility="18.75",
        tracked_keyword_count=count,
    )
    positions = []
    for index in range(count):
        position = index % depth + 1
        positions.append(
            KeywordPosition(
                ranking_snapshot=snapshot,
                query="=2+2" if index == 0 else f"демо запрос {index:04d}",
                normalized_query=f"демо запрос {index:04d}",
                frequency=index + 10,
                position_raw=str(position),
                position_value=position,
                position_status=KeywordPosition.Status.RANKED,
                group_name="@формула" if index == 0 else "Услуги",
                target_url=f"https://demo.example/services/{index}",
                normalized_target_url=f"https://demo.example/services/{index}",
            )
        )
    KeywordPosition.objects.bulk_create(positions)


@pytest.fixture
def rich_version():
    project = Project.objects.create(name="Демонстрационный проект", domain="demo.example")
    sync_synthetic_metrics(project=project, report_month=date(2026, 7, 1))
    for month in (date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)):
        _ranking(project, month)
        _ranking(project, month, depth=50, count=24, engine="yandex", region="Москва")
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    return create_report_version(report=report)


@pytest.fixture
def version():
    project = Project.objects.create(name="Демонстрационный проект", domain="plain.example")
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    return create_report_version(report=report)


def _artifact_bytes(artifact):
    with artifact.file.open("rb") as stream:
        return stream.read()


def test_full_docx_has_charts_ordered_sections_tables_and_carlito(rich_version, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    data = _artifact_bytes(
        generate_artifact(version=rich_version, artifact_type="docx", is_draft=True)
    )
    assert zipfile.is_zipfile(io.BytesIO(data))
    document = Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "ЧЕРНОВИК" in text
    headings = [p.text for p in document.paragraphs if p.style.name == "Heading 1"]
    expected = [
        {
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
        }[code]
        for code in SECTION_ORDER
    ]
    assert headings == expected
    assert document.styles["Normal"].font.name == "Carlito"
    assert document.sections[0].footer.paragraphs[0].text.startswith("demo.example")
    assert any(section.orientation == WD_ORIENT.LANDSCAPE for section in document.sections)
    assert document.sections[-1].orientation == WD_ORIENT.LANDSCAPE
    for section in document.sections:
        dimensions = (round(section.page_width.cm, 1), round(section.page_height.cm, 1))
        assert dimensions in {(21.0, 29.7), (29.7, 21.0)}
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        images = [name for name in package.namelist() if name.startswith("word/media/")]
        assert len(images) >= 10
        xml = package.read("word/document.xml")
        assert b"w:tblHeader" in xml
        assert b"w:cantSplit" in xml
        assert b"w:keepNext" in xml
    table_text = "\n".join(
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    )
    assert "Частотность" in table_text
    assert "демо запрос 0035" in table_text


@pytest.mark.parametrize(
    ("depth", "expected_ranges"),
    [
        (10, {"1-3", "4-10"}),
        (20, {"1-3", "4-10", "11-20"}),
        (30, {"1-3", "4-10", "11-20", "21-30"}),
        (50, {"1-3", "4-10", "11-20", "21-30", "31-50"}),
        (100, {"1-3", "4-10", "11-20", "21-30", "31-50", "51-100"}),
    ],
)
def test_export_respects_each_confirmed_depth(depth, expected_ranges):
    project = Project.objects.create(name=f"Depth {depth}", domain=f"depth-{depth}.example")
    _ranking(project, date(2026, 7, 31), depth=depth, count=depth)
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    segment = create_report_version(report=report).snapshot.payload["calculated"]["positions"][
        "segments"
    ][0]
    assert set(segment["distribution"]["ranges"]) == expected_ranges
    assert (segment["distribution"]["top_30"] is None) is (depth < 30)


def test_google_depth_note_is_single_and_top_30_not_rendered_for_top_20(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(name="Depth", domain="depth.example")
    _ranking(project, date(2026, 7, 31), depth=20, count=20)
    version = create_report_version(
        report=Report.objects.create(project=project, report_month=date(2026, 7, 1))
    )
    document = Document(
        io.BytesIO(
            _artifact_bytes(generate_artifact(version=version, artifact_type="docx", is_draft=True))
        )
    )
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    text += "\n" + "\n".join(
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    )
    assert text.count("Глубина проверки Google:") == 1
    assert "TOP-30" not in text


@pytest.mark.parametrize("depth", [30, 50, 100])
def test_top_30_is_rendered_only_when_confirmed(depth, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(name=f"Confirmed {depth}", domain=f"confirmed-{depth}.example")
    for month in (date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)):
        _ranking(project, month, depth=depth, count=depth)
    version = create_report_version(
        report=Report.objects.create(project=project, report_month=date(2026, 7, 1))
    )
    document = Document(
        io.BytesIO(
            _artifact_bytes(generate_artifact(version=version, artifact_type="docx", is_draft=True))
        )
    )
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    text += "\n" + "\n".join(
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    )
    assert "TOP-30" in text


@pytest.mark.parametrize(
    ("mode", "position", "present"),
    [("auto", 12, True), ("auto", 5, False), ("enabled", 5, True), ("disabled", 12, False)],
)
def test_top_11_20_project_modes(mode, position, present, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    project = Project.objects.create(
        name=f"Mode {mode}", domain=f"{mode}-{position}.example", top_11_20_mode=mode
    )
    snapshot = RankingSnapshot.objects.create(
        project=project,
        snapshot_date=date(2026, 7, 31),
        search_engine="google",
        region="Россия",
        ranking_depth=20,
        tracked_keyword_count=1,
    )
    KeywordPosition.objects.create(
        ranking_snapshot=snapshot,
        query="тест",
        normalized_query="тест",
        frequency=100,
        position_raw=str(position),
        position_value=position,
        position_status=KeywordPosition.Status.RANKED,
    )
    version = create_report_version(
        report=Report.objects.create(project=project, report_month=date(2026, 7, 1))
    )
    document = Document(
        io.BytesIO(
            _artifact_bytes(generate_artifact(version=version, artifact_type="docx", is_draft=True))
        )
    )
    headings = [p.text for p in document.paragraphs if p.style.name == "Heading 1"]
    assert ("TOP-11–20" in headings) is present


def test_xlsx_has_all_rows_native_types_and_formula_injection_protection(
    rich_version, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    workbook = load_workbook(
        io.BytesIO(
            _artifact_bytes(
                generate_artifact(version=rich_version, artifact_type="xlsx", is_draft=True)
            )
        )
    )
    assert workbook.sheetnames == [
        "Метаданные",
        "Позиции",
        "История",
        "Метрика и Вебмастер",
        "Выполненные работы",
    ]
    assert isinstance(workbook["Метаданные"]["B3"].value, date)
    positions = workbook["Позиции"]
    expected = sum(
        len(source["positions"]) for source in rich_version.snapshot.payload["ranking_sources"]
    )
    assert positions.max_row - 1 == expected
    assert positions["D2"].value == "'=2+2"
    assert positions["H2"].value == "'@формула"
    assert positions["E2"].data_type == "n"
    assert positions["C2"].is_date


def test_repeat_export_does_not_query_live_metric_sources(
    rich_version, settings, tmp_path, monkeypatch
):
    settings.MEDIA_ROOT = tmp_path

    def forbidden(*args, **kwargs):
        raise AssertionError("live metrics must not be queried during export")

    monkeypatch.setattr(RankingSnapshot.objects, "filter", forbidden)
    from apps.metrics.models import MetricPoint, SourceSnapshot

    monkeypatch.setattr(SourceSnapshot.objects, "filter", forbidden)
    monkeypatch.setattr(MetricPoint.objects, "filter", forbidden)
    first = generate_artifact(version=rich_version, artifact_type="xlsx", is_draft=True)
    second = generate_artifact(version=rich_version, artifact_type="xlsx", is_draft=True)
    assert first.status == second.status == "ready"
    first_book = load_workbook(io.BytesIO(_artifact_bytes(first)))
    second_book = load_workbook(io.BytesIO(_artifact_bytes(second)))
    assert list(first_book["Позиции"].values) == list(second_book["Позиции"].values)


def test_visibility_and_traffic_render_only_precalculated_series(rich_version, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    payload = rich_version.snapshot.payload
    segment = payload["calculated"]["positions"]["segments"][0]
    assert len(segment["three_month_series"]) == 3
    # Extra raw rows must not become chart/table points during export.
    payload["ranking_sources"].append(
        {**payload["ranking_sources"][-1], "id": "duplicate", "visibility": "999"}
    )
    traffic = payload["calculated"]["sources"]["sources"]["yandex_metrika"][
        "traffic_source_dynamics"
    ]
    expected_current = next(iter(traffic.values()))["change"]["current"]
    for source in payload["source_snapshots"]:
        if source.get("source") == "yandex_metrika":
            source["metrics"] = []
    ReportDatasetSnapshot.objects.filter(pk=rich_version.snapshot.pk).update(payload=payload)
    document = Document(
        io.BytesIO(
            _artifact_bytes(
                generate_artifact(version=rich_version, artifact_type="docx", is_draft=True)
            )
        )
    )
    table_values = [
        cell.text for table in document.tables for row in table.rows for cell in row.cells
    ]
    assert "999" not in table_values
    assert _number_text(expected_current) in table_values


def test_iks_narrative_uses_quality_index_data_instead_of_missing_fallback(rich_version):
    block = rich_version.narrative_blocks.get(section_code="iks")
    changes = rich_version.snapshot.payload["calculated"]["sources"]["sources"]["yandex_webmaster"][
        "normalized_changes"
    ]["quality_index"]
    assert block.generated_text != "Данные раздела отсутствуют."
    assert "ИКС" in block.generated_text
    assert _number_text(changes["current"]) in block.generated_text


def test_download_requires_login_and_generation_get_is_rejected(client, version):
    response = client.get(f"/versions/{version.id}/export/docx/")
    assert response.status_code == 302
    user = get_user_model().objects.create_user("exporter", password="secret-pass")
    client.force_login(user)
    response = client.get(f"/versions/{version.id}/export/docx/")
    assert response.status_code == 405


@pytest.mark.skipif(
    shutil.which("libreoffice") is None, reason="LibreOffice is tested in Docker CI"
)
def test_real_pdf_conversion_smoke(rich_version, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    artifact = generate_artifact(version=rich_version, artifact_type="pdf", is_draft=True)
    data = _artifact_bytes(artifact)
    assert data.startswith(b"%PDF-")
    pages = PdfReader(io.BytesIO(data)).pages
    assert len(pages) > 0
    allowed = ((595.28, 841.89), (841.89, 595.28))
    for page in pages:
        size = (float(page.mediabox.width), float(page.mediabox.height))
        assert any(
            abs(size[0] - width) <= 3 and abs(size[1] - height) <= 3 for width, height in allowed
        )
        assert len((page.extract_text() or "").strip()) >= 20 or list(page.images)
    texts = [page.extract_text() or "" for page in pages]
    for heading in (
        "Трафик",
        "Источники трафика",
        "Клики и показы",
        "CTR",
        "Индексация",
        "ИКС",
    ):
        section_text = next(text for text in texts if heading in text)
        for unavailable in (
            "Данные раздела отсутствуют",
            "Данные недоступны",
            "Источник недоступен",
            "Сведения об источнике недоступны",
        ):
            assert unavailable not in section_text
    for heading in ("TOP-11–20", "Все запросы отчётного периода"):
        page_text = next(text for text in texts if heading in text)
        assert "Запрос" in page_text and "Частотность" in page_text


def _number_text(value):
    return format(Decimal(str(value)).normalize(), "f").replace(".", ",")
