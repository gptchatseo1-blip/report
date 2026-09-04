from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project
from apps.yandex.crypto import CredentialConfigurationError

from .client import (
    TopvisorClient,
    TopvisorCredentials,
    TopvisorError,
    TopvisorTemporaryError,
    client_for_project,
)
from .forms import TopvisorCredentialsForm, TopvisorProjectForm, TopvisorSyncForm
from .models import TopvisorCredential, TopvisorProjectMapping, TopvisorSyncRun
from .services import configuration_id, sync_positions


def _legacy_configured():
    return bool(settings.TOPVISOR_USER_ID and settings.TOPVISOR_API_KEY)


PROJECTS_CACHE_KEY = "topvisor:projects:global"


def _annotated_configurations(items, *, project_id, project_name, primary_project_id):
    result = []
    for item in items:
        item = dict(item)
        raw_id = configuration_id(item)
        item["_topvisor_project_id"] = str(project_id)
        item["_topvisor_project_name"] = project_name
        item["_configuration_id"] = (
            raw_id if str(project_id) == str(primary_project_id) else f"{project_id}:{raw_id}"
        )
        result.append(item)
    return result


def _stored_configurations(mapping):
    if not mapping:
        return []
    return (
        _annotated_configurations(
            mapping.selected_configurations,
            project_id=mapping.topvisor_project_id,
            project_name=mapping.topvisor_project_name,
            primary_project_id=mapping.topvisor_project_id,
        )
        if not any(item.get("_topvisor_project_id") for item in mapping.selected_configurations)
        else [dict(item) for item in mapping.selected_configurations]
    )


def _configured_projects(mapping):
    grouped = {}
    for item in _stored_configurations(mapping):
        project_id = str(item.get("_topvisor_project_id") or mapping.topvisor_project_id)
        group = grouped.setdefault(
            project_id,
            {
                "id": project_id,
                "name": item.get("_topvisor_project_name")
                or mapping.topvisor_project_name
                or project_id,
                "configuration_count": 0,
            },
        )
        group["configuration_count"] += 1
    return list(grouped.values())


def _cache_projects(projects):
    projects = tuple(projects)
    cache.set(
        PROJECTS_CACHE_KEY,
        projects,
        timeout=settings.TOPVISOR_PROJECTS_CACHE_SECONDS,
    )
    return projects


def _projects_for_page(client):
    projects = cache.get(PROJECTS_CACHE_KEY)
    return projects if projects is not None else _cache_projects(client.iter_projects())


@staff_member_required
def credentials(request):
    credential = TopvisorCredential.objects.filter(pk=1).first()
    legacy_fallback = credential is None and _legacy_configured()
    has_key = bool(credential or legacy_fallback)
    action = request.POST.get("action", "")

    if request.method == "POST" and action == "delete":
        with transaction.atomic():
            TopvisorProjectMapping.objects.all().delete()
            TopvisorCredential.objects.all().delete()
        cache.delete(PROJECTS_CACHE_KEY)
        messages.success(request, "Общие реквизиты Topvisor удалены.")
        return redirect("topvisor:credentials")

    credential_error = ""
    if request.method == "POST":
        form = TopvisorCredentialsForm(request.POST, has_key=has_key)
        if form.is_valid():
            submitted_key = form.cleaned_data["api_key"]
            try:
                existing_key = settings.TOPVISOR_API_KEY if legacy_fallback else ""
                existing_key_unreadable = False
                if credential:
                    try:
                        existing_key = credential.get_api_key()
                    except CredentialConfigurationError:
                        if not submitted_key:
                            raise
                        existing_key_unreadable = True
                candidate = TopvisorCredentials(
                    form.cleaned_data["user_id"], submitted_key or existing_key
                )
                checked_projects = TopvisorClient(credentials=candidate).check_access()
                credentials_changed = bool(
                    not has_key
                    or (credential.user_id if credential else settings.TOPVISOR_USER_ID)
                    != candidate.user_id
                    or existing_key_unreadable
                    or existing_key != candidate.api_key
                )
                replacement = credential or TopvisorCredential()
                replacement.user_id = candidate.user_id
                replacement.set_api_key(candidate.api_key)
                replacement.last_verified_at = timezone.now()
                with transaction.atomic():
                    replacement.save()
                    if credentials_changed:
                        TopvisorProjectMapping.objects.all().delete()
                _cache_projects(checked_projects)
            except CredentialConfigurationError:
                credential_error = (
                    "Не удалось прочитать или зашифровать реквизиты. Проверьте ключ "
                    "шифрования либо введите API-ключ заново."
                )
            except TopvisorTemporaryError:
                credential_error = (
                    "Topvisor временно недоступен. Действующие реквизиты не изменены; "
                    "повторите попытку позже."
                )
            except TopvisorError:
                credential_error = "Не удалось проверить ID пользователя или API-ключ."
            if credential_error:
                form = TopvisorCredentialsForm(
                    {"user_id": form.cleaned_data["user_id"], "api_key": ""},
                    has_key=has_key,
                )
                form.is_valid()
                form.add_error(None, credential_error)
            else:
                message = "Общие реквизиты Topvisor сохранены и проверены."
                if credentials_changed:
                    message += " Проекты Topvisor нужно сопоставить заново."
                messages.success(request, message)
                return redirect("topvisor:credentials")
    else:
        form = TopvisorCredentialsForm(
            initial={
                "user_id": (
                    credential.user_id
                    if credential
                    else settings.TOPVISOR_USER_ID
                    if legacy_fallback
                    else ""
                )
            },
            has_key=has_key,
        )

    return render(
        request,
        "topvisor/credentials.html",
        {
            "credential": credential,
            "form": form,
            "legacy_fallback": legacy_fallback,
        },
    )


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    credential = TopvisorCredential.objects.filter(pk=1).first()
    action = request.POST.get("action") or (
        "mapping" if request.method == "POST" and request.POST.get("topvisor_project") else ""
    )
    mapping = TopvisorProjectMapping.objects.filter(project=project).first()

    if request.method == "POST" and action == "remove_project" and mapping:
        removed_id = str(request.POST.get("provider_project_id") or "")
        remaining = [
            item
            for item in _stored_configurations(mapping)
            if str(item.get("_topvisor_project_id") or mapping.topvisor_project_id) != removed_id
        ]
        if remaining:
            primary = remaining[0]
            mapping.topvisor_project_id = str(primary.get("_topvisor_project_id"))
            mapping.topvisor_project_name = str(primary.get("_topvisor_project_name") or "")
            mapping.selected_configurations = remaining
            mapping.save(
                update_fields=[
                    "topvisor_project_id",
                    "topvisor_project_name",
                    "selected_configurations",
                    "updated_at",
                ]
            )
        else:
            mapping.delete()
        messages.success(request, "Проект Topvisor удалён из подключения.")
        return redirect("topvisor:connection", project_id=project.id)

    legacy_fallback = credential is None and _legacy_configured()
    verified = bool(credential and credential.last_verified_at) or legacy_fallback
    projects, configurations, safe_error = (), (), ""
    selected = request.POST.get("topvisor_project") or request.GET.get(
        "topvisor_project", mapping.topvisor_project_id if mapping else ""
    )
    if verified:
        try:
            client, _ = client_for_project(project)
            projects = _projects_for_page(client)
            if selected:
                project_by_id = {str(item["id"]): item for item in projects}
                selected_name = (
                    project_by_id.get(str(selected), {}).get("name")
                    or project_by_id.get(str(selected), {}).get("site")
                    or str(selected)
                )
                primary_id = mapping.topvisor_project_id if mapping else selected
                configurations = tuple(
                    _annotated_configurations(
                        client.get_search_configurations(selected),
                        project_id=selected,
                        project_name=selected_name,
                        primary_project_id=primary_id,
                    )
                )
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
        initial={
            "topvisor_project": selected,
            "configurations": [
                configuration_id(item)
                for item in _stored_configurations(mapping)
                if str(item.get("_topvisor_project_id") or mapping.topvisor_project_id)
                == str(selected)
            ]
            if mapping
            else [],
        },
    )
    if request.method == "POST" and action == "mapping":
        if form.is_valid() and not safe_error and verified:
            project_by_id = {str(item["id"]): item for item in projects}
            config_by_id = {configuration_id(item): item for item in configurations}
            chosen_project = project_by_id[form.cleaned_data["topvisor_project"]]
            existing = (
                [
                    item
                    for item in _stored_configurations(mapping)
                    if str(item.get("_topvisor_project_id") or mapping.topvisor_project_id)
                    != str(chosen_project["id"])
                ]
                if mapping
                else []
            )
            chosen = [config_by_id[item] for item in form.cleaned_data["configurations"]]
            primary_id = mapping.topvisor_project_id if mapping else str(chosen_project["id"])
            primary_name = (
                mapping.topvisor_project_name if mapping else chosen_project.get("name", "")
            )
            TopvisorProjectMapping.objects.update_or_create(
                project=project,
                defaults={
                    "topvisor_project_id": str(primary_id),
                    "topvisor_project_name": primary_name,
                    "selected_configurations": [*existing, *chosen],
                },
            )
            project.position_provider = Project.PositionProvider.TOPVISOR
            project.save(update_fields=["position_provider", "updated_at"])
            messages.success(request, "Проект Topvisor и его конфигурации сохранены.")
            return redirect("topvisor:connection", project_id=project.id)

    return render(
        request,
        "topvisor/connection.html",
        {
            "project": project,
            "credential": credential,
            "mapping": mapping,
            "verified": verified,
            "legacy_fallback": legacy_fallback,
            "safe_error": safe_error,
            "form": form,
            "selected_project": selected,
            "configured_projects": _configured_projects(mapping),
            "sync_form": TopvisorSyncForm(),
            "runs": Paginator(mapping.sync_runs.all(), 10).get_page(request.GET.get("page"))
            if mapping
            else (),
        },
    )


@login_required
def sync(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    mapping = get_object_or_404(TopvisorProjectMapping, project=project)
    form = TopvisorSyncForm(request.POST)
    credential = TopvisorCredential.objects.filter(pk=1, last_verified_at__isnull=False).exists()
    if not credential and not _legacy_configured():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": False, "message": "Общие реквизиты Topvisor не настроены."}, status=400
            )
        messages.error(request, "Общие реквизиты Topvisor не настроены.")
    elif form.is_valid():
        run = sync_positions(mapping=mapping)
        if run.status == run.Status.SUCCESS:
            message = f"Topvisor: загружено позиций — {run.loaded_keyword_count}."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "message": message})
            messages.success(request, f"Загружено позиций: {run.loaded_keyword_count}.")
            if run.error_message:
                messages.warning(request, run.error_message)
            return redirect("topvisor:connection", project_id=project.id)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": False, "message": run.error_message or "Синхронизация не выполнена."},
                status=502,
            )
        messages.error(request, run.error_message)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {"ok": False, "message": "Некорректные параметры синхронизации."}, status=400
        )
    return redirect("topvisor:connection", project_id=project.id)


@login_required
def delete_run(request, project_id, run_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run = get_object_or_404(TopvisorSyncRun, pk=run_id, mapping__project_id=project_id)
    run.delete()
    messages.success(request, "Запись журнала удалена. Снимки позиций не изменены.")
    return redirect("topvisor:connection", project_id=project_id)


@login_required
def delete_failed_runs(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    TopvisorSyncRun.objects.filter(
        mapping__project_id=project_id, status=TopvisorSyncRun.Status.FAILED
    ).delete()
    messages.success(request, "Неудачные запуски удалены из журнала.")
    return redirect("topvisor:connection", project_id=project_id)
