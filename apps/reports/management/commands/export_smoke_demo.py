"""Create anonymised full-profile artifacts and verify the real PDF toolchain."""

import shutil
import subprocess
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from apps.metrics.models import KeywordPosition, RankingSnapshot
from apps.metrics.synthetic import sync_synthetic_metrics
from apps.projects.models import Project
from apps.reports.exporting import generate_artifact
from apps.reports.models import Report
from apps.reports.services import create_report_version


class Command(BaseCommand):
    help = "Generate anonymised DOCX/PDF/XLSX and perform a real PDF smoke check."

    def add_arguments(self, parser):
        parser.add_argument("--output", default="demo-artifacts")

    def handle(self, *args, **options):
        if not shutil.which("libreoffice") or not shutil.which("pdftoppm"):
            raise CommandError("LibreOffice and pdftoppm are required")
        output = Path(options["output"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        project, _ = Project.objects.get_or_create(
            normalized_domain="seo-demo.invalid",
            defaults={"name": "Обезличенный демонстрационный проект", "domain": "seo-demo.invalid"},
        )
        sync_synthetic_metrics(project=project, report_month=date(2026, 7, 1))
        if not project.ranking_snapshots.exists():
            for month in (date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)):
                ranking = RankingSnapshot.objects.create(
                    project=project,
                    snapshot_date=month,
                    search_engine="google",
                    region="Россия",
                    ranking_depth=50,
                    visibility="24.50",
                    tracked_keyword_count=50,
                )
                KeywordPosition.objects.bulk_create(
                    [
                        KeywordPosition(
                            ranking_snapshot=ranking,
                            query=f"обезличенный запрос {index}",
                            normalized_query=f"обезличенный запрос {index}",
                            frequency=100 + index,
                            position_raw=str(index),
                            position_value=index,
                            position_status=KeywordPosition.Status.RANKED,
                            group_name="Демо",
                            target_url=f"https://seo-demo.invalid/page/{index}",
                            normalized_target_url=f"https://seo-demo.invalid/page/{index}",
                        )
                        for index in range(1, 51)
                    ]
                )
        report, _ = Report.objects.get_or_create(project=project, report_month=date(2026, 7, 1))
        version = create_report_version(report=report)
        generated = {}
        for kind in ("docx", "pdf", "xlsx"):
            artifact = generate_artifact(version=version, artifact_type=kind, is_draft=True)
            destination = output / f"seo-demo.{kind}"
            with artifact.file.open("rb") as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            generated[kind] = destination
        pdf_data = generated["pdf"].read_bytes()
        if not pdf_data.startswith(b"%PDF-"):
            raise CommandError("PDF signature is invalid")
        reader = PdfReader(generated["pdf"])
        if not reader.pages:
            raise CommandError("PDF has no pages")
        sparse_pages = [
            number
            for number, page in enumerate(reader.pages, start=1)
            if len((page.extract_text() or "").strip()) < 20 and not list(page.images)
        ]
        if sparse_pages:
            raise CommandError(f"PDF contains unexpectedly empty pages: {sparse_pages}")
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                "1",
                "-singlefile",
                "-png",
                str(generated["pdf"]),
                str(output / "seo-demo"),
            ],
            check=True,
            timeout=120,
        )
        png = output / "seo-demo.png"
        if not png.exists() or png.stat().st_size == 0:
            raise CommandError("PDF raster preview was not created")
        document = Document(generated["docx"])
        if not document.inline_shapes:
            raise CommandError("DOCX has no charts")
        load_workbook(generated["xlsx"], read_only=True).close()
        self.stdout.write(
            self.style.SUCCESS(f"PDF smoke passed: {len(reader.pages)} pages; artifacts: {output}")
        )
