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
)
from .forms import TopvisorCredentialsForm, TopvisorProjectForm, TopvisorSyncForm
from .models import TopvisorCredential, TopvisorProjectMapping, TopvisorSyncRun
from .services import configuration_id, sync_positions


def _legacy_configured():
    return bool(settings.TOPVISOR_USER_ID and settings.TOPVISOR_API_KEY)


PROJECTS_CACHE_KEY = "topvisor:projects"


def _annotated_configurations(
    items,
    *,
    project_id,
    project_name,
    primary_project_id,
    credential_id=None,
    credential_user_id="",
):
    result = []
    for item in items:
        item = dict(item)
        raw_id = configuration_id(item)
        item["_topvisor_project_id"] = str(project_id)
        item["_topvisor_project_name"] = project_name
        if credential_id is not None:
            item["_topvisor_credential_id"] = str(credential_id)
            item["_topvisor_user_id"] = credential_user_id
            item["_configuration_id"] = f"{credential_id}:{project_id}:{raw_id}"
        else:
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
            credential_id=mapping.topvisor_credential_id,
            credential_user_id=(
                mapping.topvisor_credential.user_id if mapping.topvisor_credential else ""
            ),
        )
        if not any(item.get("_topvisor_project_id") for item in mapping.selected_configurations)
        else [dict(item) for item in mapping.selected_configurations]
    )


def _configured_projects(mapping):
    grouped = {}
    for item in _stored_configurations(mapping):
        project_id = str(item.get("_topvisor_project_id") or mapping.topvisor_project_id)
        credential_id = str(
            item.get("_topvisor_credential_id") or mapping.topvisor_credential_id or "legacy"
        )
        selector_id = f"{credential_id}:{project_id}"
        group = grouped.setdefault(
            selector_id,
            {
                "id": project_id,
                "selector_id": selector_id,
                "name": item.get("_topvisor_project_name")
                or mapping.topvisor_project_name
                or project_id,
                "account": item.get("_topvisor_user_id")
                or (
                    mapping.topvisor_credential.user_id if mapping.topvisor_credential else "legacy"
                ),
                "configuration_count": 0,
            },
        )
        group["configuration_count"] += 1
    return list(grouped.values())


def _projects_cache_key(credential_id):
    return f"{PROJECTS_CACHE_KEY}:{credential_id}"


def _cache_projects(credential_id, projects):
    projects = tuple(projects)
    cache.set(
        _projects_cache_key(credential_id),
        projects,
        timeout=settings.TOPVISOR_PROJECTS_CACHE_SECONDS,
    )
    return projects


def _projects_for_page(client, credential_id):
    projects = cache.get(_projects_cache_key(credential_id))
    return (
        projects if projects is not None else _cache_projects(credential_id, client.iter_projects())
    )


def _annotated_projects(projects, credential_id, user_id):
    return tuple(
        {
            **item,
            "_topvisor_project_id": str(item["id"]),
            "_topvisor_credential_id": str(credential_id),
            "_topvisor_user_id": user_id,
            "_selector_id": f"{credential_id}:{item['id']}",
        }
        for item in projects
    )


def _configuration_account_id(mapping, configuration):
    return str(
        configuration.get("_topvisor_credential_id") or mapping.topvisor_credential_id or "legacy"
    )


def _configuration_project_selector(mapping, configuration):
    project_id = configuration.get("_topvisor_project_id") or mapping.topvisor_project_id
    return f"{_configuration_account_id(mapping, configuration)}:{project_id}"


def _remove_credential_from_mappings(credential):
    sole_credential = TopvisorCredential.objects.count() == 1
    for mapping in TopvisorProjectMapping.objects.select_related("topvisor_credential"):
        stored = _stored_configurations(mapping)

        remaining = [
            item
            for item in stored
            if (
                str(credential.pk)
                if sole_credential
                and not item.get("_topvisor_credential_id")
                and not mapping.topvisor_credential_id
                else _configuration_account_id(mapping, item)
            )
            != str(credential.pk)
        ]
        if not remaining:
            mapping.delete()
            continue
        primary = remaining[0]
        primary_credential_id = primary.get("_topvisor_credential_id")
        mapping.topvisor_project_id = str(primary.get("_topvisor_project_id"))
        mapping.topvisor_project_name = str(primary.get("_topvisor_project_name") or "")
        mapping.topvisor_credential_id = (
            int(primary_credential_id)
            if primary_credential_id and str(primary_credential_id).isdigit()
            else None
        )
        mapping.selected_configurations = remaining
        mapping.save(
            update_fields=[
                "topvisor_project_id",
                "topvisor_project_name",
                "topvisor_credential",
                "selected_configurations",
                "updated_at",
            ]
        )


@staff_member_required
def credentials(request):
    credentials_list = list(TopvisorCredential.objects.order_by("user_id", "pk"))
    legacy_fallback = not credentials_list and _legacy_configured()
    action = request.POST.get("action", "")
    selected_id = request.POST.get("credential_id") or request.GET.get("credential")
    credential = next(
        (item for item in credentials_list if str(item.pk) == str(selected_id)),
        None,
    )
    if (
        request.method == "POST"
        and action == "credentials"
        and not request.POST.get("create_new")
        and not selected_id
        and len(credentials_list) == 1
    ):
        # Preserve requests from the former single-account form.  The current
        # interface sends create_new=1 when a second account is being added.
        credential = credentials_list[0]
    has_key = bool(credential or (legacy_fallback and not selected_id))

    if request.method == "POST" and action == "delete":
        if credential is None and len(credentials_list) == 1:
            credential = credentials_list[0]
        if credential is None:
            messages.error(request, "Аккаунт Topvisor не найден.")
            return redirect("topvisor:credentials")
        with transaction.atomic():
            _remove_credential_from_mappings(credential)
            credential.delete()
        cache.delete(_projects_cache_key(credential.pk))
        messages.success(request, "Аккаунт Topvisor удалён.")
        return redirect("topvisor:credentials")

    credential_error = ""
    if request.method == "POST":
        form = TopvisorCredentialsForm(request.POST, has_key=has_key)
        if form.is_valid():
            submitted_key = form.cleaned_data["api_key"]
            try:
                existing_key = settings.TOPVISOR_API_KEY if legacy_fallback else ""
                original_user_id = credential.user_id if credential else ""
                if credential:
                    try:
                        existing_key = credential.get_api_key()
                    except CredentialConfigurationError:
                        if not submitted_key:
                            raise
                candidate = TopvisorCredentials(
                    form.cleaned_data["user_id"], submitted_key or existing_key
                )
                duplicate = TopvisorCredential.objects.filter(user_id=candidate.user_id)
                if credential:
                    duplicate = duplicate.exclude(pk=credential.pk)
                if duplicate.exists():
                    raise TopvisorError("Этот ID пользователя Topvisor уже подключён.")
                checked_projects = TopvisorClient(credentials=candidate).check_access()
                replacement = credential or TopvisorCredential()
                replacement.user_id = candidate.user_id
                replacement.set_api_key(candidate.api_key)
                replacement.last_verified_at = timezone.now()
                with transaction.atomic():
                    replacement.save()
                    if credential and original_user_id != candidate.user_id:
                        _remove_credential_from_mappings(replacement)
                cache.delete(_projects_cache_key(replacement.pk))
                _cache_projects(replacement.pk, checked_projects)
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
                    {
                        "credential_id": credential.pk if credential else "",
                        "user_id": form.cleaned_data["user_id"],
                        "api_key": "",
                    },
                    has_key=has_key,
                )
                form.is_valid()
                form.add_error(None, credential_error)
            else:
                message = "Аккаунт Topvisor сохранён и проверен."
                messages.success(request, message)
                return redirect("topvisor:credentials")
    else:
        form = TopvisorCredentialsForm(
            initial={
                "credential_id": credential.pk if credential else "",
                "user_id": (
                    credential.user_id
                    if credential
                    else settings.TOPVISOR_USER_ID
                    if legacy_fallback
                    else ""
                ),
            },
            has_key=has_key,
        )

    return render(
        request,
        "topvisor/credentials.html",
        {
            "credential": credential,
            "credentials": credentials_list,
            "form": form,
            "legacy_fallback": legacy_fallback,
        },
    )


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    credentials_list = list(
        TopvisorCredential.objects.filter(last_verified_at__isnull=False).order_by("user_id", "pk")
    )
    action = request.POST.get("action") or (
        "mapping" if request.method == "POST" and request.POST.get("topvisor_project") else ""
    )
    mapping = (
        TopvisorProjectMapping.objects.select_related("topvisor_credential")
        .filter(project=project)
        .first()
    )

    if request.method == "POST" and action == "remove_project" and mapping:
        removed_selector = str(request.POST.get("provider_project") or "")
        removed_id = str(request.POST.get("provider_project_id") or "")
        remaining = [
            item
            for item in _stored_configurations(mapping)
            if (
                _configuration_project_selector(mapping, item) != removed_selector
                if removed_selector
                else str(item.get("_topvisor_project_id") or mapping.topvisor_project_id)
                != removed_id
            )
        ]
        if remaining:
            primary = remaining[0]
            mapping.topvisor_project_id = str(primary.get("_topvisor_project_id"))
            mapping.topvisor_project_name = str(primary.get("_topvisor_project_name") or "")
            primary_credential_id = primary.get("_topvisor_credential_id")
            mapping.topvisor_credential_id = (
                int(primary_credential_id)
                if primary_credential_id and str(primary_credential_id).isdigit()
                else None
            )
            mapping.selected_configurations = remaining
            mapping.save(
                update_fields=[
                    "topvisor_project_id",
                    "topvisor_project_name",
                    "topvisor_credential",
                    "selected_configurations",
                    "updated_at",
                ]
            )
        else:
            mapping.delete()
        messages.success(request, "Проект Topvisor удалён из подключения.")
        return redirect("topvisor:connection", project_id=project.id)

    legacy_fallback = not credentials_list and _legacy_configured()
    verified = bool(credentials_list) or legacy_fallback
    projects, configurations = [], ()
    account_errors = []
    clients_by_account = {}
    for credential in credentials_list:
        try:
            account_client = TopvisorClient(
                credentials=TopvisorCredentials(credential.user_id, credential.get_api_key())
            )
            clients_by_account[str(credential.pk)] = account_client
            projects.extend(
                _annotated_projects(
                    _projects_for_page(account_client, credential.pk),
                    credential.pk,
                    credential.user_id,
                )
            )
        except CredentialConfigurationError:
            account_errors.append(
                f"Не удалось прочитать сохранённые реквизиты аккаунта {credential.user_id}."
            )
        except TopvisorTemporaryError:
            account_errors.append(f"Аккаунт {credential.user_id}: Topvisor временно недоступен.")
        except TopvisorError:
            account_errors.append(f"Аккаунт {credential.user_id}: проекты получить не удалось.")
    if legacy_fallback:
        legacy_client = TopvisorClient()
        clients_by_account["legacy"] = legacy_client
        try:
            projects.extend(
                _annotated_projects(
                    _projects_for_page(legacy_client, "legacy"),
                    "legacy",
                    settings.TOPVISOR_USER_ID,
                )
            )
        except TopvisorError:
            account_errors.append("Проекты Topvisor из настроек сервера получить не удалось.")

    stored = _stored_configurations(mapping)
    default_selected = ""
    if stored:
        first = stored[0]
        default_selected = (
            f"{first.get('_topvisor_credential_id') or mapping.topvisor_credential_id or 'legacy'}:"
            f"{first.get('_topvisor_project_id') or mapping.topvisor_project_id}"
        )
    selected = request.POST.get("topvisor_project") or request.GET.get(
        "topvisor_project", default_selected
    )
    project_by_id = {str(item["_selector_id"]): item for item in projects}
    if selected and selected not in project_by_id:
        legacy_matches = [
            item for item in projects if str(item.get("_topvisor_project_id")) == str(selected)
        ]
        if len(legacy_matches) == 1:
            selected = str(legacy_matches[0]["_selector_id"])
    if selected and selected in project_by_id:
        try:
            chosen_project = project_by_id[selected]
            account_id = str(chosen_project["_topvisor_credential_id"])
            provider_project_id = str(chosen_project["_topvisor_project_id"])
            selected_name = (
                chosen_project.get("name") or chosen_project.get("site") or provider_project_id
            )
            primary_id = mapping.topvisor_project_id if mapping else provider_project_id
            configurations = tuple(
                _annotated_configurations(
                    clients_by_account[account_id].get_search_configurations(provider_project_id),
                    project_id=provider_project_id,
                    project_name=selected_name,
                    primary_project_id=primary_id,
                    credential_id=account_id,
                    credential_user_id=str(chosen_project.get("_topvisor_user_id") or ""),
                )
            )
        except CredentialConfigurationError:
            account_errors.append("Не удалось прочитать API-ключ выбранного аккаунта.")
        except TopvisorTemporaryError:
            account_errors.append("Topvisor временно недоступен. Повторите попытку позже.")
        except TopvisorError:
            account_errors.append("Не удалось получить конфигурации выбранного проекта.")

    safe_error = " ".join(dict.fromkeys(account_errors))

    form_data = request.POST.copy() if action == "mapping" else None
    if form_data is not None:
        form_data["topvisor_project"] = selected
        configuration_ids = {configuration_id(item) for item in configurations}
        normalized_configuration_ids = []
        for posted_id in form_data.getlist("configurations"):
            if posted_id in configuration_ids:
                normalized_configuration_ids.append(posted_id)
                continue
            matches = [
                configuration_id(item)
                for item in configurations
                if str(item.get("id")) == str(posted_id)
                or configuration_id(item).endswith(f":{posted_id}")
            ]
            normalized_configuration_ids.append(matches[0] if len(matches) == 1 else posted_id)
        form_data.setlist("configurations", normalized_configuration_ids)

    form = TopvisorProjectForm(
        form_data,
        projects=projects,
        configurations=configurations,
        initial={
            "topvisor_project": selected,
            "configurations": [
                configuration_id(item)
                for item in stored
                if _configuration_project_selector(mapping, item) == str(selected)
            ]
            if mapping
            else [],
        },
    )
    if request.method == "POST" and action == "mapping":
        if form.is_valid() and selected in project_by_id and verified:
            config_by_id = {configuration_id(item): item for item in configurations}
            chosen_project = project_by_id[form.cleaned_data["topvisor_project"]]
            chosen_selector = str(chosen_project["_selector_id"])
            existing = (
                [
                    item
                    for item in stored
                    if _configuration_project_selector(mapping, item) != chosen_selector
                ]
                if mapping
                else []
            )
            chosen = [config_by_id[item] for item in form.cleaned_data["configurations"]]
            provider_project_id = str(chosen_project["_topvisor_project_id"])
            primary_id = mapping.topvisor_project_id if mapping else provider_project_id
            primary_name = (
                mapping.topvisor_project_name if mapping else chosen_project.get("name", "")
            )
            primary_credential_id = (
                mapping.topvisor_credential_id
                if mapping
                else int(chosen_project["_topvisor_credential_id"])
                if str(chosen_project["_topvisor_credential_id"]).isdigit()
                else None
            )
            TopvisorProjectMapping.objects.update_or_create(
                project=project,
                defaults={
                    "topvisor_project_id": str(primary_id),
                    "topvisor_project_name": primary_name,
                    "topvisor_credential_id": primary_credential_id,
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
            "credentials": credentials_list,
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
    credential = TopvisorCredential.objects.filter(last_verified_at__isnull=False).exists()
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
