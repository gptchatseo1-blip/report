from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import PositionImportForm
from .models import ImportBatch
from .parser import ImportFileError
from .services import ImportConfirmationError, confirm_import, create_import_preview


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
