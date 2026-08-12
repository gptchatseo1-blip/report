from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project
from apps.reports.models import Report

from .client import TopvisorClient, TopvisorCredentials, TopvisorError, client_for_project
from .forms import TopvisorCredentialsForm, TopvisorProjectForm, TopvisorSyncForm
from .models import TopvisorConnection, TopvisorProjectMapping
from .services import configuration_id, sync_positions


def _legacy_configured():
    return bool(settings.TOPVISOR_USER_ID and settings.TOPVISOR_API_KEY)


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    credential = TopvisorConnection.objects.filter(project=project).first()
    action = request.POST.get("action") or (
        "mapping" if request.POST.get("topvisor_project") else ""
    )
    mapping = TopvisorProjectMapping.objects.filter(project=project).first()

    if request.method == "POST" and action == "credentials":
        credential_form = TopvisorCredentialsForm(request.POST, has_key=bool(credential))
        if credential_form.is_valid():
            api_key = credential_form.cleaned_data["api_key"]
            if not api_key and credential:
                api_key = credential.get_api_key()
            candidate = TopvisorCredentials(credential_form.cleaned_data["user_id"], api_key)
            try:
                projects = tuple(TopvisorClient(credentials=candidate).check_access())
            except TopvisorError:
                # Rebuild the form so the submitted secret cannot be echoed in HTML.
                credential_form = TopvisorCredentialsForm(
                    {"user_id": credential_form.cleaned_data["user_id"], "api_key": ""},
                    has_key=True,
                )
                credential_form.is_valid()
                credential_form.add_error(
                    None, "Не удалось проверить ID пользователя или API-ключ."
                )
            else:
                credential, _ = TopvisorConnection.objects.get_or_create(
                    project=project,
                    defaults={"user_id": candidate.user_id, "api_key_encrypted": b"pending"},
                )
                credential.user_id = candidate.user_id
                if (
                    credential_form.cleaned_data["api_key"]
                    or credential.api_key_encrypted == b"pending"
                ):
                    credential.set_api_key(candidate.api_key)
                credential.last_verified_at = timezone.now()
                credential.save()
                messages.success(request, "Реквизиты Topvisor сохранены и проверены.")
                return redirect("topvisor:connection", project_id=project.id)
    else:
        credential_form = TopvisorCredentialsForm(
            initial={"user_id": credential.user_id if credential else ""},
            has_key=bool(credential),
        )

    legacy_fallback = credential is None and _legacy_configured()
    verified = bool(credential and credential.last_verified_at) or legacy_fallback
    projects, configurations, safe_error = (), (), ""
    selected = request.POST.get("topvisor_project") or request.GET.get(
        "topvisor_project", mapping.topvisor_project_id if mapping else ""
    )
    if verified:
        client, _ = client_for_project(project)
        try:
            projects = tuple(client.iter_projects())
            if selected:
                configurations = tuple(client.get_search_configurations(selected))
        except TopvisorError:
            safe_error = "Не удалось получить проекты Topvisor. Проверьте подключение."

    form = TopvisorProjectForm(
        request.POST if action == "mapping" else None,
        projects=projects,
        configurations=configurations,
    )
    if request.method == "POST" and action == "mapping":
        if form.is_valid() and not safe_error and verified:
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
            messages.success(request, "Проект Topvisor и конфигурации сохранены.")
            return redirect("topvisor:connection", project_id=project.id)

    return render(
        request,
        "topvisor/connection.html",
        {
            "project": project,
            "connection": credential,
            "credential_form": credential_form,
            "mapping": mapping,
            "verified": verified,
            "legacy_fallback": legacy_fallback,
            "safe_error": safe_error,
            "form": form,
            "selected_project": selected,
            "sync_form": TopvisorSyncForm(),
            "runs": mapping.sync_runs.all()[:10] if mapping else (),
        },
    )


@login_required
def delete_connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    credential = TopvisorConnection.objects.filter(project=project).first()
    if request.method == "POST":
        if credential:
            credential.delete()
        messages.success(request, "Подключение Topvisor удалено.")
        return redirect("topvisor:connection", project_id=project.id)
    return render(
        request, "topvisor/delete_connection.html", {"project": project, "connection": credential}
    )


@login_required
def sync(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    mapping = get_object_or_404(TopvisorProjectMapping, project=project)
    form = TopvisorSyncForm(request.POST)
    credential = TopvisorConnection.objects.filter(
        project=project, last_verified_at__isnull=False
    ).exists()
    if not credential and not _legacy_configured():
        messages.error(request, "Реквизиты Topvisor не настроены для проекта.")
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
