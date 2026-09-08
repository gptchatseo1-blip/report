import hashlib
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.metrics.models import KeywordPosition, RankingSnapshot

from .models import FileImportSegment, ImportBatch, ImportRowError
from .parser import ImportFileError, parse_position_file, parse_position_history_xlsx


class ImportConfirmationError(Exception):
    pass


def create_import_preview(
    *, project, uploaded_file, snapshot_date, search_engine, region, user, ranking_depth=100
):
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
        "ranking_depth": ranking_depth,
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
        return batch.ranking_snapshots.order_by("snapshot_date").first(), False
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
        depth_raw=str(batch.ranking_depth),
        ranking_depth=batch.ranking_depth,
        depth_source=RankingSnapshot.DepthSource.MANUAL,
    )
    KeywordPosition.objects.bulk_create(
        [KeywordPosition(ranking_snapshot=snapshot, **row) for row in batch.preview_payload],
        batch_size=1000,
    )
    batch.status = ImportBatch.Status.IMPORTED
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_at", "updated_at"])
    return snapshot, True


@transaction.atomic
def import_history_file(
    *, project, search_engine, region, uploaded_file, calculate_visibility, user, segment=None
):
    """Normalize every XLSX date column into an idempotent RankingSnapshot."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ImportFileError(f"Файл больше {settings.MAX_UPLOAD_SIZE_MB} МБ.")
    filename = Path(uploaded_file.name).name
    data = uploaded_file.read()
    uploaded_file.seek(0)
    preview = parse_position_history_xlsx(filename, data)
    region = " ".join(region.split())[:120]
    if not region:
        raise ImportFileError("Укажите регион.")
    conflict = FileImportSegment.objects.filter(
        project=project, search_engine=search_engine, region=region
    )
    if segment and segment.pk:
        conflict = conflict.exclude(pk=segment.pk)
    if conflict.exists():
        raise ImportFileError("Такой сегмент уже существует. Используйте «Обновить файл».")

    segment = segment or FileImportSegment(project=project)
    segment.search_engine = search_engine
    segment.region = region
    segment.calculate_visibility = bool(calculate_visibility)
    segment.status = ImportBatch.Status.PREVIEW
    segment.error_message = ""
    segment.save()
    RankingSnapshot.objects.filter(
        project=project, topvisor_configuration_id=segment.configuration_id
    ).update(search_engine=search_engine, region=region)
    checksum = hashlib.sha256(data).hexdigest()
    existing = ImportBatch.objects.filter(
        segment=segment,
        kind=ImportBatch.Kind.FILE_POSITIONS,
        file_checksum=checksum,
        status=ImportBatch.Status.IMPORTED,
    ).first()
    created = existing is None
    batch = existing or ImportBatch.objects.create(
        project=project,
        segment=segment,
        kind=ImportBatch.Kind.FILE_POSITIONS,
        original_filename=filename,
        source_file=ContentFile(data, name=filename),
        file_checksum=checksum,
        status=ImportBatch.Status.PREVIEW,
        snapshot_date=preview.dates[-1],
        search_engine=search_engine,
        region=region,
        ranking_depth=100,
        total_rows=preview.total_rows,
        valid_rows=preview.keyword_count,
        error_rows=0,
        date_count=len(preview.dates),
        preview_payload={"dates": [day.isoformat() for day in preview.dates]},
        uploaded_by=user,
    )
    from apps.topvisor.services import calculate_visibility

    configuration_id = segment.configuration_id
    for snapshot_date in preview.dates:
        rows = preview.rows_by_date[snapshot_date]
        visibility = (
            calculate_visibility(
                [{"frequency": row["frequency"], "position": row["position_value"]} for row in rows]
            )
            if segment.calculate_visibility
            else None
        )
        snapshot, _ = RankingSnapshot.objects.update_or_create(
            project=project,
            snapshot_date=snapshot_date,
            topvisor_configuration_id=configuration_id,
            defaults={
                "import_batch": batch,
                "search_engine": search_engine,
                "region": region,
                "tracked_keyword_count": len(rows),
                "ranking_depth": 100,
                "depth_raw": "100",
                "depth_source": RankingSnapshot.DepthSource.FILE_IMPORT,
                "visibility": visibility,
                "visibility_raw": {
                    "source": (
                        "file_import_calculated_visibility"
                        if segment.calculate_visibility
                        else "file_import_visibility_disabled"
                    ),
                    "value": str(visibility) if visibility is not None else None,
                },
                "response_checksum": checksum,
                "retrieved_at": timezone.now(),
                "provenance": {
                    "method": "file_import",
                    "segment_id": str(segment.id),
                    "filename": filename,
                },
            },
        )
        snapshot.positions.all().delete()
        KeywordPosition.objects.bulk_create(
            [KeywordPosition(ranking_snapshot=snapshot, **row) for row in rows], batch_size=1000
        )
    batch.status = ImportBatch.Status.IMPORTED
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_at", "updated_at"])
    segment.last_filename = filename
    segment.imported_at = timezone.now()
    segment.keyword_count = preview.keyword_count
    segment.date_count = len(preview.dates)
    segment.status = ImportBatch.Status.IMPORTED
    segment.save(
        update_fields=[
            "last_filename",
            "imported_at",
            "keyword_count",
            "date_count",
            "status",
            "error_message",
            "updated_at",
        ]
    )
    return segment, batch, created
