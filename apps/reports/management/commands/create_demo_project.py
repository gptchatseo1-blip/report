from django.core.management.base import BaseCommand

from apps.reports.demo import create_demo_project


class Command(BaseCommand):
    help = "Idempotently create the anonymised, offline MVP-1 demonstration project."

    def handle(self, *args, **options):
        project, report, version = create_demo_project()
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo ready: project={project.id}; report={report.id}; "
                f"version={version.number}; snapshot={version.snapshot.checksum}"
            )
        )
