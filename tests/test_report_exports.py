import io
import shutil
import zipfile
from datetime import date

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
from apps.reports.models import Report
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
    assert document.sections[-1].orientation == WD_ORIENT.PORTRAIT
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        images = [name for name in package.namelist() if name.startswith("word/media/")]
        assert len(images) >= 10
        xml = package.read("word/document.xml")
        assert b"w:tblHeader" in xml
        assert b"w:cantSplit" in xml
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
    assert text.count("Глубина проверки Google:") == 1
    assert "TOP-30 находится" not in text


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
    assert len(PdfReader(io.BytesIO(data)).pages) > 0
