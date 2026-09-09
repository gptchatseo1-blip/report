import os
import sys
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.yandex.models import YandexMetrikaSyncRun
from apps.yandex.services import execute_queued_metrika_sync


class Command(BaseCommand):
    help = "Process queued Yandex.Metrika synchronization jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        while True:
            YandexMetrikaSyncRun.objects.filter(
                status=YandexMetrikaSyncRun.Status.RUNNING,
                started_at__lt=timezone.now() - timedelta(minutes=30),
            ).update(
                status=YandexMetrikaSyncRun.Status.FAILED,
                completed_at=timezone.now(),
                error_message="Синхронизация прервана. Запустите её повторно.",
            )
            run = (
                YandexMetrikaSyncRun.objects.filter(status=YandexMetrikaSyncRun.Status.QUEUED)
                .order_by("started_at")
                .first()
            )
            if run:
                claimed = YandexMetrikaSyncRun.objects.filter(
                    pk=run.pk, status=YandexMetrikaSyncRun.Status.QUEUED
                ).update(status=YandexMetrikaSyncRun.Status.RUNNING)
                if claimed:
                    self.stdout.write(f"Processing Metrika sync run {run.pk}")
                    execute_queued_metrika_sync(run.pk)
                    if not options["once"]:
                        os.execv(sys.executable, [sys.executable, *sys.argv])
            elif options["once"]:
                return
            else:
                time.sleep(max(0.2, options["poll_seconds"]))
            if options["once"]:
                return
