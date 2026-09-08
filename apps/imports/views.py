from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.metrics.models import RankingSnapshot
from apps.projects.models import Project

from .forms import FileImportSegmentForm, PositionImportForm
from .models import FileImportSegment, ImportBatch
from .parser import ImportFileError
from .services import (
    ImportConfirmationError,
    confirm_import,
    create_import_preview,
    import_history_file,
)


@staff_member_required
def import_template(request):
    content = "\ufeffЗапрос;Позиция;Частотность;Группа;URL\r\n"
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="topvisor_positions_template.csv"'
    return response


@staff_member_required
def import_list(request):
    batches = ImportBatch.objects.select_related("project", "uploaded_by")[:100]
    return render(request, "imports/list.html", {"batches": batches})


@staff_member_required
def import_upload(request):
    form = PositionImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch, created = create_import_preview(
                project=form.cleaned_data["project"],
                uploaded_file=form.cleaned_data["source_file"],
                snapshot_date=form.cleaned_data["snapshot_date"],
                search_engine=form.cleaned_data["search_engine"],
                region=form.cleaned_data["region"],
                ranking_depth=form.cleaned_data["ranking_depth"],
                user=request.user,
            )
        except ImportFileError as exc:
            form.add_error("source_file", str(exc))
        else:
            if created:
                messages.success(request, "Файл проверен. Просмотрите результат перед импортом.")
            else:
                messages.info(request, "Этот файл уже загружался с такими параметрами.")
            return redirect("imports:detail", batch_id=batch.id)
    return render(request, "imports/upload.html", {"form": form})


@staff_member_required
def import_detail(request, batch_id):
    batch = get_object_or_404(
        ImportBatch.objects.select_related("project", "uploaded_by"), pk=batch_id
    )
    errors = batch.row_errors.all()[:200]
    return render(
        request,
        "imports/detail.html",
        {"batch": batch, "errors": errors, "preview_rows": batch.preview_payload[:50]},
    )


@staff_member_required
@require_POST
def import_confirm(request, batch_id):
    batch = get_object_or_404(ImportBatch, pk=batch_id)
    try:
        snapshot, created = confirm_import(batch.id)
    except ImportConfirmationError as exc:
        messages.error(request, str(exc))
    else:
        if created:
            messages.success(
                request,
                f"Импортировано позиций: {snapshot.tracked_keyword_count}.",
            )
        else:
            messages.info(request, "Эта партия уже была импортирована.")
    return redirect("imports:detail", batch_id=batch.id)


@login_required
def project_import_settings(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    edit_segment = None
    if request.GET.get("edit"):
        edit_segment = get_object_or_404(FileImportSegment, pk=request.GET["edit"], project=project)
    form = FileImportSegmentForm(request.POST or None, request.FILES or None, instance=edit_segment)
    if request.method == "POST" and form.is_valid():
        try:
            segment, _batch, created = import_history_file(
                project=project,
                segment=edit_segment,
                search_engine=form.cleaned_data["search_engine"],
                region=form.cleaned_data["region"],
                uploaded_file=form.cleaned_data["source_file"],
                calculate_visibility=form.cleaned_data["calculate_visibility"],
                user=request.user,
            )
        except ImportFileError as exc:
            form.add_error("source_file", str(exc))
        else:
            project.position_provider = Project.PositionProvider.FILE_IMPORT
            project.save(update_fields=["position_provider", "updated_at"])
            messages.success(
                request,
                (
                    f"Файл обработан. Найдено {segment.keyword_count} строк, "
                    f"{segment.date_count} дат."
                    if created
                    else "Этот файл уже импортирован; данные не дублировались."
                ),
            )
            return redirect("imports:project-settings", project_id=project.id)
    return render(
        request,
        "imports/project_settings.html",
        {
            "project": project,
            "segments": project.file_import_segments.all(),
            "form": form,
            "edit_segment": edit_segment,
            "provider_choices": Project.PositionProvider.choices,
        },
    )


@login_required
@require_POST
def project_import_delete(request, project_id, segment_id):
    project = get_object_or_404(Project, pk=project_id)
    segment = get_object_or_404(FileImportSegment, pk=segment_id, project=project)
    RankingSnapshot.objects.filter(
        project=project, topvisor_configuration_id=segment.configuration_id
    ).delete()
    segment.delete()
    messages.success(request, "Сегмент импорта удалён. Остальные сегменты не изменены.")
    return redirect("imports:project-settings", project_id=project.id)


@login_required
@require_POST
def select_position_provider(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    provider = request.POST.get("position_provider")
    if provider not in Project.PositionProvider.values:
        return HttpResponse("Некорректный источник позиций.", status=400)
    previous_provider = project.position_provider
    project.position_provider = provider
    project.save(update_fields=["position_provider", "updated_at"])
    if (
        provider == Project.PositionProvider.FILE_IMPORT
        and previous_provider != Project.PositionProvider.FILE_IMPORT
    ):
        from apps.reports.models import ProjectReportSettings

        settings_row, _ = ProjectReportSettings.objects.get_or_create(project=project)
        settings_row.values = {**(settings_row.values or {}), "include_visibility_table": True}
        settings_row.save(update_fields=["values", "updated_at"])
    messages.success(request, f"Источник позиций: {project.get_position_provider_display()}.")
    if provider == Project.PositionProvider.FILE_IMPORT:
        return redirect("imports:project-settings", project_id=project.id)
    return redirect("reports:report-list", project_id=project.id)
