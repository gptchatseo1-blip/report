import hashlib
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot

from .models import ImportBatch, ImportRowError
from .parser import ImportFileError, parse_position_file


class ImportConfirmationError(Exception):
    pass


def create_import_preview(*, project, uploaded_file, snapshot_date, search_engine, region, user):
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ImportFileError(f"Файл больше {settings.MAX_UPLOAD_SIZE_MB} МБ.")

    filename = Path(uploaded_file.name).name
    extension = Path(filename).suffix.casefold()
    if extension not in {".csv", ".xlsx"}:
        raise ImportFileError("Поддерживаются только файлы CSV и XLSX.")

    data = uploaded_file.read()
    uploaded_file.seek(0)
    checksum = hashlib.sha256(data).hexdigest()
    lookup = {
        "project": project,
        "kind": ImportBatch.Kind.TOPVISOR_POSITIONS,
        "file_checksum": checksum,
        "snapshot_date": snapshot_date,
        "search_engine": search_engine,
        "region": region.strip(),
    }
    existing = ImportBatch.objects.filter(**lookup).first()
    if existing:
        return existing, False

    preview = parse_position_file(filename, data, snapshot_date=snapshot_date)
    try:
        with transaction.atomic():
            batch = ImportBatch.objects.create(
                **lookup,
                original_filename=filename,
                source_file=ContentFile(data, name=filename),
                status=ImportBatch.Status.PREVIEW,
                total_rows=preview.total_rows,
                valid_rows=len(preview.valid_rows),
                error_rows=preview.error_row_count,
                preview_payload=preview.valid_rows,
                uploaded_by=user,
            )
            ImportRowError.objects.bulk_create(
                [
                    ImportRowError(
                        batch=batch,
                        row_number=error.row_number,
                        code=error.code,
                        message=error.message,
                        raw_values=error.raw_values,
                    )
                    for error in preview.errors
                ],
                batch_size=1000,
            )
    except IntegrityError:
        return ImportBatch.objects.get(**lookup), False
    return batch, True


@transaction.atomic
def confirm_import(batch_id):
    batch = ImportBatch.objects.select_for_update().select_related("project").get(pk=batch_id)
    if batch.status == ImportBatch.Status.IMPORTED:
        return batch.ranking_snapshot, False
    if batch.status != ImportBatch.Status.PREVIEW:
        raise ImportConfirmationError("Эта партия не ожидает подтверждения.")
    if batch.error_rows:
        raise ImportConfirmationError("Исправьте ошибки строк и загрузите файл повторно.")
    if not batch.valid_rows:
        raise ImportConfirmationError("В партии нет корректных строк для импорта.")

    snapshot = RankingSnapshot.objects.create(
        project=batch.project,
        import_batch=batch,
        snapshot_date=batch.snapshot_date,
        search_engine=batch.search_engine,
        region=batch.region,
        tracked_keyword_count=batch.valid_rows,
    )
    KeywordPosition.objects.bulk_create(
        [KeywordPosition(ranking_snapshot=snapshot, **row) for row in batch.preview_payload],
        batch_size=1000,
    )
    batch.status = ImportBatch.Status.IMPORTED
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_at", "updated_at"])
    return snapshot, True
