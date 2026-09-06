import hashlib
import secrets
import urllib.parse
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.metrics.models import SourceSnapshot
from apps.projects.models import Project, normalize_domain

from .client import MetrikaClient, WebmasterClient, YandexAPIError, exchange_token
from .credentials import get_oauth_credentials
from .crypto import CredentialConfigurationError, encrypt_token
from .forms import CounterForm, GoalsForm, HostForm, SyncForm, YandexOAuthCredentialsForm
from .models import (
    YandexConnection,
    YandexMetrikaProjectMapping,
    YandexMetrikaSyncRun,
    YandexOAuthCredential,
    YandexOAuthState,
    YandexWebmasterProjectMapping,
    YandexWebmasterSyncRun,
)
from .services import sync_metrika, sync_webmaster

METRIKA_SCOPE = "metrika:read"
WEBMASTER_SCOPES = ("webmaster:hostinfo", "webmaster:verify")
OAUTH_SCOPES = (METRIKA_SCOPE, *WEBMASTER_SCOPES)


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _period_word(count):
    if count % 10 == 1 and count % 100 != 11:
        return "период"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return "периода"
    return "периодов"


def _sync_message(run):
    fetched = getattr(run, "fetched_period_count", 0)
    reused = getattr(run, "reused_period_count", 0)
    if not fetched:
        message = "Все выбранные периоды уже синхронизированы."
    else:
        message = (
            "Загружен 1 новый период"
            if fetched == 1
            else f"Загружено {fetched} новых {_period_word(fetched)}"
        )
        if reused:
            message += (
                ", использован 1 сохранённый период"
                if reused == 1
                else f", использованы {reused} сохранённых {_period_word(reused)}"
            )
        message += "."
    unavailable = len(getattr(run, "unavailable_goal_ids", []))
    if unavailable:
        message += f" Недоступные цели пропущены: {unavailable}."
    return message


def _sync_json(mapping, source, run):
    if run.status != run.Status.SUCCESS:
        return JsonResponse(
            {"ok": False, "message": run.error_message or "Синхронизация не выполнена."},
            status=400,
        )
    periods = [
        {
            "id": str(row.id),
            "month": row.period_start.strftime("%Y-%m"),
            "label": f"{row.period_start:%d.%m.%Y} — {row.period_end:%d.%m.%Y}",
        }
        for row in SourceSnapshot.objects.filter(project=mapping.project, source=source).order_by(
            "-period_start", "-period_end", "id"
        )
    ]
    return JsonResponse(
        {
            "ok": True,
            "message": _sync_message(run),
            "last_synced_at": timezone.localtime(
                mapping.last_successful_sync_at or timezone.now()
            ).strftime("%d.%m.%Y %H:%M"),
            "periods": periods,
        }
    )


def _is_other_domain(project, value):
    try:
        return normalize_domain(value) != project.normalized_domain
    except (ValidationError, TypeError, ValueError):
        return True


def _counter_options(project, counters):
    options = []
    for counter in counters:
        site2 = counter.get("site2") if isinstance(counter.get("site2"), dict) else {}
        domain = str(site2.get("site") or counter.get("site") or "")
        name = str(counter.get("name") or domain or f"Счётчик {counter.get('id', '')}")
        options.append(
            {
                "id": str(counter.get("id", "")),
                "label": f"{name} — {domain}" if domain and domain != name else name,
                "mismatch": _is_other_domain(project, domain),
            }
        )
    return options


def _host_options(project, hosts):
    options = []
    for host in hosts:
        url = str(host.get("unicode_host_url") or host.get("ascii_host_url") or "")
        options.append(
            {
                "id": str(host.get("host_id", "")),
                "label": url or str(host.get("host_id", "")),
                "verified": host.get("verified") is True,
                "mismatch": _is_other_domain(project, url),
            }
        )
    return options


def _configured():
    if not settings.CREDENTIAL_ENCRYPTION_KEY:
        return False
    try:
        return get_oauth_credentials() is not None
    except CredentialConfigurationError:
        return False


@staff_member_required
def oauth_credentials(request):
    record = YandexOAuthCredential.objects.filter(pk=1).first()
    try:
        current = get_oauth_credentials()
        credential_error = ""
    except CredentialConfigurationError:
        current = None
        credential_error = "Сохранённый Client secret невозможно расшифровать. Введите его заново."
    has_secret = not credential_error and (
        bool(record and record.client_secret_encrypted)
        or bool(not record and current and current.client_secret)
    )
    if request.method == "POST":
        form = YandexOAuthCredentialsForm(request.POST, has_secret=has_secret)
        if form.is_valid():
            submitted_secret = form.cleaned_data["client_secret"]
            client_changed = not current or current.client_id != form.cleaned_data["client_id"]
            secret_changed = bool(
                submitted_secret and (not current or submitted_secret != current.client_secret)
            )
            replacement = record or YandexOAuthCredential()
            replacement.client_id = form.cleaned_data["client_id"]
            replacement.redirect_uri = form.cleaned_data["redirect_uri"]
            try:
                if submitted_secret:
                    replacement.set_client_secret(submitted_secret)
                elif not record and current:
                    replacement.set_client_secret(current.client_secret)
                with transaction.atomic():
                    replacement.save()
                    if client_changed or secret_changed:
                        YandexConnection.objects.filter(active=True).update(
                            active=False,
                            access_token_encrypted=b"",
                            refresh_token_encrypted=None,
                        )
            except CredentialConfigurationError:
                form.add_error(
                    None,
                    "Не удалось зашифровать Client secret. Проверьте ключ шифрования сервиса.",
                )
            else:
                if client_changed or secret_changed:
                    messages.success(
                        request,
                        "Реквизиты OAuth Яндекса сохранены. Аккаунты проектов нужно "
                        "авторизовать заново.",
                    )
                else:
                    messages.success(request, "Реквизиты OAuth Яндекса сохранены.")
                return redirect("yandex:oauth-credentials")
    else:
        form = YandexOAuthCredentialsForm(
            initial={
                "client_id": record.client_id if record else (current.client_id if current else ""),
                "redirect_uri": (
                    record.redirect_uri
                    if record
                    else (
                        current.redirect_uri
                        if current
                        else request.build_absolute_uri(reverse("yandex:oauth-callback"))
                    )
                ),
            },
            has_secret=has_secret,
        )
    return render(
        request,
        "yandex/oauth_credentials.html",
        {
            "form": form,
            "credential": record,
            "legacy_fallback": bool(current and current.legacy and not record),
            "credential_error": credential_error,
        },
    )


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    mapping = (
        YandexMetrikaProjectMapping.objects.filter(
            project=project, connection__user=request.user, connection__active=True
        )
        .select_related("connection")
        .first()
    )
    webmaster_mapping = (
        YandexWebmasterProjectMapping.objects.filter(
            project=project, connection__user=request.user, connection__active=True
        )
        .select_related("connection")
        .first()
    )
    counters = hosts = []
    error = ""
    connection_obj = (
        mapping.connection
        if mapping
        else (
            webmaster_mapping.connection
            if webmaster_mapping
            else YandexConnection.objects.filter(user=request.user, active=True).first()
        )
    )
    if connection_obj:
        try:
            client = MetrikaClient(connection_obj)
            counters = list(client.counters())
        except (YandexAPIError, CredentialConfigurationError):
            error = "Не удалось получить данные Яндекс Метрики."
        if all(scope in connection_obj.scopes for scope in WEBMASTER_SCOPES):
            try:
                webmaster = WebmasterClient(connection_obj)
                webmaster_user = webmaster.user()
                webmaster_user_id = webmaster_user.get("user_id") or webmaster_user.get("id")
                hosts = list(webmaster.hosts(webmaster_user_id)) if webmaster_user_id else []
            except (YandexAPIError, CredentialConfigurationError):
                error = "Не удалось получить данные Яндекс Вебмастера."
    return render(
        request,
        "yandex/connection.html",
        {
            "project": project,
            "mapping": mapping,
            "connection": connection_obj,
            "counter_options": _counter_options(project, counters),
            "host_options": _host_options(project, hosts),
            "webmaster_mapping": webmaster_mapping,
            "webmaster_scope_missing": bool(
                connection_obj
                and any(scope not in connection_obj.scopes for scope in WEBMASTER_SCOPES)
            ),
            "error": error,
            "configured": _configured(),
            "runs": mapping.sync_runs.all()[:10] if mapping else [],
            "webmaster_runs": webmaster_mapping.sync_runs.all()[:10] if webmaster_mapping else [],
        },
    )


@login_required
def oauth_start(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    try:
        credentials = get_oauth_credentials() if _configured() else None
    except CredentialConfigurationError:
        credentials = None
    if not credentials:
        messages.error(request, "OAuth Яндекса не настроен.")
        return redirect("yandex:connection", project_id=project.id)
    if not request.session.session_key:
        request.session.create()
    raw = secrets.token_urlsafe(32)
    YandexOAuthState.objects.create(
        digest=hashlib.sha256(raw.encode()).hexdigest(),
        user=request.user,
        session_key=request.session.session_key,
        project=project,
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": credentials.client_id,
            "redirect_uri": credentials.redirect_uri,
            "scope": " ".join(OAUTH_SCOPES),
            "force_confirm": "yes",
            "state": raw,
        }
    )
    return redirect(f"{settings.YANDEX_OAUTH_AUTHORIZE_URL}?{query}")


def consume_oauth_state(*, raw, user, session_key):
    """Atomically consume a state; only the transaction winner may exchange its code."""
    digest = hashlib.sha256(raw.encode()).hexdigest()
    cutoff = timezone.now() - timedelta(minutes=10)
    with transaction.atomic():
        state = (
            YandexOAuthState.objects.select_for_update()
            .select_related("project")
            .filter(digest=digest)
            .first()
        )
        if (
            state is None
            or state.user_id != user.id
            or state.session_key != session_key
            or state.created_at < cutoff
            or state.used_at is not None
        ):
            return None
        consumed_at = timezone.now()
        updated = YandexOAuthState.objects.filter(pk=state.pk, used_at__isnull=True).update(
            used_at=consumed_at
        )
        if updated != 1:
            return None
        state.used_at = consumed_at
        return state


@login_required
def oauth_callback(request):
    raw, code = request.GET.get("state", ""), request.GET.get("code", "")
    if request.GET.get("error"):
        state = consume_oauth_state(
            raw=raw, user=request.user, session_key=request.session.session_key
        )
        if state is None:
            return HttpResponseBadRequest("Недействительный или просроченный OAuth state.")
        messages.error(
            request,
            "Яндекс не выдал необходимые права. Проверьте доступы Метрики и Вебмастера "
            "в настройках OAuth-приложения и повторите авторизацию.",
        )
        return redirect("yandex:connection", project_id=state.project_id)
    if not raw or not code:
        return HttpResponseBadRequest("Недействительный или просроченный OAuth state.")
    state = consume_oauth_state(raw=raw, user=request.user, session_key=request.session.session_key)
    if state is None:
        return HttpResponseBadRequest("Недействительный или просроченный OAuth state.")
    try:
        credentials = get_oauth_credentials()
        if not credentials:
            raise YandexAPIError("OAuth Яндекса не настроен.")
        token = exchange_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": credentials.redirect_uri,
            },
            credentials=credentials,
        )
        expires = (
            timezone.now() + timedelta(seconds=int(token["expires_in"]))
            if token.get("expires_in")
            else None
        )
        raw_scopes = token.get("scope") or OAUTH_SCOPES
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
        defaults = {
            "account_id": str(token.get("uid", "")),
            "account_login": str(token.get("login", "")),
            "access_token_encrypted": encrypt_token(token["access_token"]),
            "refresh_token_encrypted": (
                encrypt_token(token["refresh_token"]) if token.get("refresh_token") else None
            ),
            "expires_at": expires,
            "scopes": scopes,
            "active": True,
        }
        project_connection_id = (
            YandexMetrikaProjectMapping.objects.filter(
                project=state.project, connection__user=request.user
            )
            .values_list("connection_id", flat=True)
            .first()
            or YandexWebmasterProjectMapping.objects.filter(
                project=state.project, connection__user=request.user
            )
            .values_list("connection_id", flat=True)
            .first()
        )
        existing = YandexConnection.objects.filter(user=request.user, active=True).first()
        if existing is None and project_connection_id:
            existing = YandexConnection.objects.filter(pk=project_connection_id).first()
        if existing:
            for field, value in defaults.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*defaults, "updated_at"])
        else:
            existing = YandexConnection.objects.create(user=request.user, **defaults)
        YandexMetrikaProjectMapping.objects.filter(
            project=state.project, connection__user=request.user
        ).update(connection=existing)
        YandexWebmasterProjectMapping.objects.filter(
            project=state.project, connection__user=request.user
        ).update(connection=existing)
    except (YandexAPIError, CredentialConfigurationError):
        messages.error(request, "Не удалось завершить OAuth-подключение Яндекса.")
        return redirect("yandex:connection", project_id=state.project_id)
    messages.success(request, "Аккаунт Яндекса подключён.")
    return redirect("yandex:connection", project_id=state.project_id)


@login_required
def select_counter(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    form = CounterForm(request.POST)
    connection_obj = get_object_or_404(
        YandexConnection, pk=request.POST.get("connection_id"), user=request.user, active=True
    )
    if not form.is_valid():
        return HttpResponseBadRequest("Некорректный счётчик.")
    data = form.cleaned_data
    try:
        counter = MetrikaClient(connection_obj).counter(data["counter_id"])
        if not counter or str(counter.get("id", "")) != str(data["counter_id"]):
            raise YandexAPIError("Счётчик недоступен для подключённого аккаунта.")
        counter_name = str(counter.get("name") or "")
        site2 = counter.get("site2") if isinstance(counter.get("site2"), dict) else {}
        counter_domain = str(site2.get("site") or counter.get("site") or "")
        if not counter_domain:
            raise YandexAPIError("Метрика не вернула домен счётчика.")
        normalized_counter_domain = normalize_domain(counter_domain)
    except (YandexAPIError, CredentialConfigurationError, ValidationError, TypeError, ValueError):
        messages.error(request, "Не удалось проверить выбранный счётчик Метрики.")
        return redirect("yandex:connection", project_id=project.id)
    mismatch = normalized_counter_domain != project.normalized_domain
    if mismatch and not data["confirm_domain_mismatch"]:
        messages.error(
            request, "Домен счётчика отличается от домена проекта. Подтвердите выбор явно."
        )
        return redirect("yandex:connection", project_id=project.id)
    YandexMetrikaProjectMapping.objects.update_or_create(
        project=project,
        defaults={
            "connection": connection_obj,
            "counter_id": str(counter["id"]),
            "counter_name": counter_name,
            "counter_domain": counter_domain,
            "domain_mismatch_confirmed": mismatch,
            "selected_goals": [],
        },
    )
    messages.success(request, "Счётчик Яндекс.Метрики сохранён.")
    return redirect("yandex:connection", project_id=project.id)


@login_required
def select_goals(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    mapping = get_object_or_404(
        YandexMetrikaProjectMapping,
        project_id=project_id,
        connection__user=request.user,
        connection__active=True,
    )
    try:
        available = list(MetrikaClient(mapping.connection).goals(mapping.counter_id))
    except (YandexAPIError, CredentialConfigurationError):
        messages.error(request, "Не удалось получить цели выбранного счётчика.")
        return redirect(
            "reports:report-list"
            if request.POST.get("return_to_reports") == "1"
            else "yandex:connection",
            project_id=project_id,
        )
    form = GoalsForm(request.POST, available_goals=available)
    if not form.is_valid():
        return HttpResponseBadRequest("Некорректные цели.")
    selected = set(form.cleaned_data["goals"])
    previous = {str(g["id"]): g for g in mapping.selected_goals}
    mapping.selected_goals = []
    for goal in available:
        goal_id = str(goal["id"])
        if goal_id not in selected:
            continue
        condition = next(
            (
                str(item.get("url") or "")
                for item in (goal.get("conditions") or [])
                if item.get("url")
            ),
            "",
        )
        selected_goal = {
            "id": goal_id,
            "name": goal.get("name", ""),
            "label": previous.get(goal_id, {}).get("label", goal.get("name", "")),
        }
        identifier = goal.get("identifier") or condition
        if identifier:
            selected_goal["identifier"] = identifier
        mapping.selected_goals.append(selected_goal)
    mapping.save(update_fields=["selected_goals", "updated_at"])
    messages.success(
        request, "Цели Метрики сохранены. Новая настройка применится при синхронизации."
    )
    return redirect(
        "reports:report-list"
        if request.POST.get("return_to_reports") == "1"
        else "yandex:connection",
        project_id=project_id,
    )


@login_required
def select_host(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    connection_obj = get_object_or_404(
        YandexConnection, pk=request.POST.get("connection_id"), user=request.user, active=True
    )
    if any(scope not in connection_obj.scopes for scope in WEBMASTER_SCOPES):
        messages.error(request, "Требуется повторная авторизация с правом Вебмастера.")
        return redirect("yandex:connection", project_id=project.id)
    form = HostForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Некорректный сайт.")
    try:
        webmaster = WebmasterClient(connection_obj)
        user_response = webmaster.user()
        user_id = user_response.get("user_id") or user_response.get("id")
        available = {str(item.get("host_id")): item for item in webmaster.hosts(user_id)}
        host = available.get(form.cleaned_data["host_id"])
        if not host:
            raise YandexAPIError("Сайт недоступен.")
        if host.get("verified") is not True:
            messages.error(
                request,
                "Сайт не подтверждён в Яндекс Вебмастере. Его нельзя выбрать для синхронизации.",
            )
            return redirect("yandex:connection", project_id=project.id)
        host_url = str(host.get("ascii_host_url") or host.get("unicode_host_url") or "")
        host_domain = normalize_domain(host_url)
    except (YandexAPIError, CredentialConfigurationError, ValidationError, TypeError, ValueError):
        messages.error(request, "Не удалось проверить выбранный сайт Вебмастера.")
        return redirect("yandex:connection", project_id=project.id)
    mismatch = host_domain != project.normalized_domain
    if mismatch and not form.cleaned_data["confirm_domain_mismatch"]:
        messages.error(request, "Домен сайта отличается от домена проекта. Подтвердите выбор явно.")
        return redirect("yandex:connection", project_id=project.id)
    YandexWebmasterProjectMapping.objects.update_or_create(
        project=project,
        defaults={
            "connection": connection_obj,
            "host_id": str(host["host_id"]),
            "host_url": host_url,
            "verification_status": "VERIFIED" if host.get("verified") is True else "UNVERIFIED",
            "main_mirror": str(
                (host.get("main_mirror") or {}).get("ascii_host_url")
                or (host.get("main_mirror") or {}).get("unicode_host_url")
                or ""
            ),
            "domain_mismatch_confirmed": mismatch,
        },
    )
    messages.success(request, "Сайт Яндекс.Вебмастера сохранён.")
    return redirect("yandex:connection", project_id=project.id)


@login_required
def sync(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    mapping = get_object_or_404(
        YandexMetrikaProjectMapping,
        project_id=project_id,
        connection__user=request.user,
        connection__active=True,
    )
    form = SyncForm(request.POST)
    if not form.is_valid():
        if _is_ajax(request):
            return JsonResponse(
                {"ok": False, "message": "Выберите корректный месяц синхронизации."},
                status=400,
            )
        return HttpResponseBadRequest("Некорректный месяц.")
    run = sync_metrika(
        mapping=mapping,
        report_month=form.cleaned_data["month"],
        user=request.user,
        force_refresh=form.cleaned_data["force_refresh"],
    )
    mapping.refresh_from_db(fields=["last_successful_sync_at"])
    if _is_ajax(request):
        return _sync_json(mapping, SourceSnapshot.Source.METRIKA, run)
    if run.status == run.Status.SUCCESS:
        messages.success(request, _sync_message(run))
        if request.POST.get("return_to_reports") == "1":
            from apps.reports.models import Report

            Report.objects.get_or_create(
                project=mapping.project, report_month=form.cleaned_data["month"].replace(day=1)
            )
            return redirect("reports:report-list", project_id=mapping.project_id)
        return redirect("yandex:connection", project_id=project_id)
    messages.error(request, run.error_message)
    if request.POST.get("return_to_reports") == "1":
        return redirect("reports:report-list", project_id=mapping.project_id)
    return redirect("yandex:connection", project_id=project_id)


@login_required
def sync_webmaster_view(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    mapping = get_object_or_404(
        YandexWebmasterProjectMapping,
        project_id=project_id,
        connection__user=request.user,
        connection__active=True,
    )
    form = SyncForm(request.POST)
    if not form.is_valid():
        if _is_ajax(request):
            return JsonResponse(
                {"ok": False, "message": "Выберите корректный месяц синхронизации."},
                status=400,
            )
        return HttpResponseBadRequest("Некорректный месяц.")
    run = sync_webmaster(
        mapping=mapping,
        report_month=form.cleaned_data["month"],
        user=request.user,
        force_refresh=form.cleaned_data["force_refresh"],
    )
    mapping.refresh_from_db(fields=["last_successful_sync_at"])
    if _is_ajax(request):
        return _sync_json(mapping, SourceSnapshot.Source.WEBMASTER, run)
    if run.status == run.Status.SUCCESS:
        messages.success(request, _sync_message(run))
        if request.POST.get("return_to_reports") == "1":
            from apps.reports.models import Report

            Report.objects.get_or_create(
                project=mapping.project, report_month=form.cleaned_data["month"].replace(day=1)
            )
            return redirect("reports:report-list", project_id=mapping.project_id)
        return redirect("yandex:connection", project_id=project_id)
    messages.error(request, run.error_message)
    if request.POST.get("return_to_reports") == "1":
        return redirect("reports:report-list", project_id=mapping.project_id)
    return redirect("yandex:connection", project_id=project_id)


@login_required
def delete_metrika_run(request, project_id, run_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run = get_object_or_404(
        YandexMetrikaSyncRun,
        pk=run_id,
        mapping__project_id=project_id,
        mapping__connection__user=request.user,
    )
    run.delete()
    messages.success(request, "Запись синхронизации Метрики удалена.")
    return redirect("yandex:connection", project_id=project_id)


@login_required
def delete_webmaster_run(request, project_id, run_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    run = get_object_or_404(
        YandexWebmasterSyncRun,
        pk=run_id,
        mapping__project_id=project_id,
        mapping__connection__user=request.user,
    )
    run.delete()
    messages.success(request, "Запись синхронизации Вебмастера удалена.")
    return redirect("yandex:connection", project_id=project_id)


@login_required
def disconnect(request, connection_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    obj = get_object_or_404(YandexConnection, pk=connection_id, user=request.user)
    project_id = request.POST.get("project_id")
    obj.active = False
    obj.access_token_encrypted = b""
    obj.refresh_token_encrypted = None
    obj.save(
        update_fields=["active", "access_token_encrypted", "refresh_token_encrypted", "updated_at"]
    )
    messages.success(request, "Аккаунт Яндекса отключён.")
    return redirect("yandex:connection", project_id=project_id)
