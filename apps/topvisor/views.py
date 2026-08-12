from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project
from apps.reports.models import Report
from apps.yandex.crypto import CredentialConfigurationError

from .client import (
    TopvisorClient,
    TopvisorCredentials,
    TopvisorError,
    TopvisorTemporaryError,
    client_for_project,
)
from .forms import TopvisorCredentialsForm, TopvisorProjectForm, TopvisorSyncForm
from .models import TopvisorConnection, TopvisorProjectMapping
from .services import configuration_id, sync_positions


def _legacy_configured():
    return bool(settings.TOPVISOR_USER_ID and settings.TOPVISOR_API_KEY)


def _projects_cache_key(project):
    return f"topvisor:projects:{project.pk}"


def _cache_projects(project, projects):
    projects = tuple(projects)
    cache.set(
        _projects_cache_key(project),
        projects,
        timeout=settings.TOPVISOR_PROJECTS_CACHE_SECONDS,
    )
    return projects


def _projects_for_page(project, client):
    projects = cache.get(_projects_cache_key(project))
    return projects if projects is not None else _cache_projects(project, client.iter_projects())


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    credential = TopvisorConnection.objects.filter(project=project).first()
    action = request.POST.get("action") or (
        "mapping" if request.POST.get("topvisor_project") else ""
    )
    mapping = TopvisorProjectMapping.objects.filter(project=project).first()
    credential_error = ""

    if request.method == "POST" and action == "credentials":
        credential_form = TopvisorCredentialsForm(request.POST, has_key=bool(credential))
        if credential_form.is_valid():
            submitted_key = credential_form.cleaned_data["api_key"]
            try:
                existing_key = ""
                existing_key_unreadable = False
                if credential:
                    try:
                        existing_key = credential.get_api_key()
                    except CredentialConfigurationError:
                        if not submitted_key:
                            raise
                        existing_key_unreadable = True
                api_key = submitted_key or existing_key
                candidate = TopvisorCredentials(credential_form.cleaned_data["user_id"], api_key)
                checked_projects = TopvisorClient(credentials=candidate).check_access()
                credentials_changed = bool(
                    not credential
                    or credential.user_id != candidate.user_id
                    or existing_key_unreadable
                    or existing_key != candidate.api_key
                )
                replacement = TopvisorConnection(project=project, user_id=candidate.user_id)
                replacement.set_api_key(candidate.api_key)
                replacement.last_verified_at = timezone.now()
                with transaction.atomic():
                    if credential:
                        credential.user_id = replacement.user_id
                        credential.api_key_encrypted = replacement.api_key_encrypted
                        credential.api_key_last_four = replacement.api_key_last_four
                        credential.last_verified_at = replacement.last_verified_at
                        credential.save()
                    else:
                        replacement.save()
                        credential = replacement
                    if credentials_changed:
                        TopvisorProjectMapping.objects.filter(project=project).delete()
                _cache_projects(project, checked_projects)
            except CredentialConfigurationError:
                credential_error = (
                    "Не удалось прочитать сохранённые реквизиты. Проверьте ключ шифрования "
                    "или сохраните подключение заново"
                )
            except TopvisorTemporaryError:
                credential_error = (
                    "Topvisor временно недоступен. Действующие реквизиты не изменены; "
                    "повторите попытку позже."
                )
            except TopvisorError:
                credential_error = "Не удалось проверить ID пользователя или API-ключ."
            if credential_error:
                # Rebuild the bound form so a submitted secret can never be echoed in HTML.
                credential_form = TopvisorCredentialsForm(
                    {"user_id": credential_form.cleaned_data["user_id"], "api_key": ""},
                    has_key=True,
                )
                credential_form.is_valid()
                credential_form.add_error(None, credential_error)
            else:
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
    if verified and not credential_error:
        try:
            client, _ = client_for_project(project)
            projects = _projects_for_page(project, client)
            if selected:
                configurations = tuple(client.get_search_configurations(selected))
        except CredentialConfigurationError:
            safe_error = (
                "Не удалось прочитать сохранённые реквизиты. Проверьте ключ шифрования "
                "или сохраните подключение заново"
            )
        except TopvisorTemporaryError:
            safe_error = "Topvisor временно недоступен. Повторите попытку позже."
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
        with transaction.atomic():
            TopvisorProjectMapping.objects.filter(project=project).delete()
            if credential:
                credential.delete()
        cache.delete(_projects_cache_key(project))
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
