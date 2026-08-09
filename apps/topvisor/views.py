from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from apps.projects.models import Project
from apps.reports.models import Report

from .client import TopvisorClient, TopvisorError
from .forms import TopvisorProjectForm, TopvisorSyncForm
from .models import TopvisorProjectMapping
from .services import configuration_id, sync_positions


def _configured():
    return bool(settings.TOPVISOR_USER_ID and settings.TOPVISOR_API_KEY)


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    mapping = TopvisorProjectMapping.objects.filter(project=project).first()
    projects, configurations, safe_error = (), (), ""
    selected = request.POST.get("topvisor_project") or request.GET.get(
        "topvisor_project", mapping.topvisor_project_id if mapping else ""
    )
    if _configured():
        client = TopvisorClient()
        try:
            projects = tuple(client.iter_projects())
            if selected:
                configurations = tuple(client.get_search_configurations(selected))
        except TopvisorError as exc:
            safe_error = str(exc)
    form = TopvisorProjectForm(
        request.POST or None, projects=projects, configurations=configurations
    )
    if request.method == "POST" and form.is_valid() and not safe_error:
        project_by_id = {str(item["id"]): item for item in projects}
        config_by_id = {configuration_id(item): item for item in configurations}
        chosen_project = project_by_id[form.cleaned_data["topvisor_project"]]
        TopvisorProjectMapping.objects.update_or_create(
            project=project,
            defaults={
                "topvisor_project_id": str(chosen_project["id"]),
                "topvisor_project_name": chosen_project.get("name", ""),
                "selected_configurations": [
                    config_by_id[item] for item in form.cleaned_data["configurations"]
                ],
            },
        )
        messages.success(request, "Topvisor подключён; конфигурации сохранены.")
        return redirect("topvisor:connection", project_id=project.id)
    return render(request, "topvisor/connection.html", {
        "project": project, "mapping": mapping, "configured": _configured(),
        "safe_error": safe_error, "form": form, "selected_project": selected,
        "sync_form": TopvisorSyncForm(), "runs": mapping.sync_runs.all()[:10] if mapping else (),
    })


@login_required
def sync(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    mapping = get_object_or_404(TopvisorProjectMapping, project=project)
    form = TopvisorSyncForm(request.POST)
    if not _configured():
        messages.error(request, "Реквизиты Topvisor не настроены на сервере.")
    elif form.is_valid():
        run = sync_positions(mapping=mapping, report_month=form.cleaned_data["month"])
        if run.status == run.Status.SUCCESS:
            report, _ = Report.objects.get_or_create(
                project=project, report_month=form.cleaned_data["month"]
            )
            messages.success(request, f"Загружено позиций: {run.loaded_keyword_count}.")
            return redirect("reports:report-detail", report_id=report.id)
        messages.error(request, run.error_message)
    return redirect("topvisor:connection", project_id=project.id)
