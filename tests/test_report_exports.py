import io
import zipfile
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from docx import Document
from openpyxl import load_workbook

from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import Report
from apps.reports.narratives import SECTION_ORDER
from apps.reports.services import create_report_version

pytestmark = pytest.mark.django_db


@pytest.fixture
def version():
    project = Project.objects.create(name="Демонстрационный проект", domain="demo.example")
    report = Report.objects.create(project=project, report_month=date(2026, 7, 1))
    return create_report_version(report=report)


def test_draft_docx_is_valid_marked_and_has_ordered_sections(version, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    artifact = generate_artifact(version=version, artifact_type="docx", is_draft=True)
    data = artifact.file.read()
    assert zipfile.is_zipfile(io.BytesIO(data))
    document = Document(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "ЧЕРНОВИК" in text
    headings = [p.text for p in document.paragraphs if p.style.name == "Heading 1"][:-1]
    assert len(headings) == len(SECTION_ORDER)
    assert document.styles["Normal"].font.name == "Carlito"
    assert document.sections[0].footer.paragraphs[0].text.startswith("demo.example")


def test_xlsx_has_required_sheets_and_native_types(version, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    artifact = generate_artifact(version=version, artifact_type="xlsx", is_draft=True)
    workbook = load_workbook(io.BytesIO(artifact.file.read()))
    assert workbook.sheetnames == [
        "Метаданные",
        "Позиции",
        "История",
        "Метрика и Вебмастер",
        "Выполненные работы",
    ]
    assert isinstance(workbook["Метаданные"]["B3"].value, date)


def test_download_requires_login_and_generation_get_is_rejected(client, version):
    response = client.get(f"/versions/{version.id}/export/docx/")
    assert response.status_code == 302
    user = get_user_model().objects.create_user("exporter", password="secret-pass")
    client.force_login(user)
    response = client.get(f"/versions/{version.id}/export/docx/")
    assert response.status_code == 405
