import hashlib
import secrets
import urllib.parse
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project, normalize_domain

from .client import MetrikaClient, YandexAPIError, exchange_token
from .crypto import CredentialConfigurationError, encrypt_token
from .forms import CounterForm, GoalsForm, SyncForm
from .models import YandexConnection, YandexMetrikaProjectMapping, YandexOAuthState
from .services import sync_metrika


def _configured():
    return all(
        (
            settings.YANDEX_CLIENT_ID,
            settings.YANDEX_CLIENT_SECRET,
            settings.YANDEX_REDIRECT_URI,
            settings.CREDENTIAL_ENCRYPTION_KEY,
        )
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
    counters = goals = []
    error = ""
    connection_obj = (
        mapping.connection
        if mapping
        else YandexConnection.objects.filter(user=request.user, active=True).first()
    )
    if connection_obj:
        try:
            client = MetrikaClient(connection_obj)
            counters = list(client.counters())
            if mapping:
                goals = list(client.goals(mapping.counter_id))
        except (YandexAPIError, CredentialConfigurationError) as exc:
            error = str(exc)
    return render(
        request,
        "yandex/connection.html",
        {
            "project": project,
            "mapping": mapping,
            "connection": connection_obj,
            "counters": counters,
            "goals": goals,
            "error": error,
            "configured": _configured(),
            "runs": mapping.sync_runs.all()[:10] if mapping else [],
        },
    )


@login_required
def oauth_start(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    if not _configured():
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
            "client_id": settings.YANDEX_CLIENT_ID,
            "redirect_uri": settings.YANDEX_REDIRECT_URI,
            "scope": "metrika:read",
            "state": raw,
        }
    )
    return redirect(f"{settings.YANDEX_OAUTH_AUTHORIZE_URL}?{query}")


@login_required
def oauth_callback(request):
    raw, code = request.GET.get("state", ""), request.GET.get("code", "")
    state = (
        YandexOAuthState.objects.filter(
            digest=hashlib.sha256(raw.encode()).hexdigest(),
            user=request.user,
            session_key=request.session.session_key,
            used_at__isnull=True,
            created_at__gte=timezone.now() - timedelta(minutes=10),
        )
        .select_related("project")
        .first()
    )
    if not state or not code:
        return HttpResponseBadRequest("Недействительный или просроченный OAuth state.")
    state.used_at = timezone.now()
    state.save(update_fields=["used_at"])
    try:
        token = exchange_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.YANDEX_REDIRECT_URI,
            }
        )
    except (YandexAPIError, CredentialConfigurationError) as exc:
        messages.error(request, str(exc))
        return redirect("yandex:connection", project_id=state.project_id)
    expires = (
        timezone.now() + timedelta(seconds=int(token["expires_in"]))
        if token.get("expires_in")
        else None
    )
    YandexConnection.objects.create(
        user=request.user,
        account_id=str(token.get("uid", "")),
        account_login=str(token.get("login", "")),
        access_token_encrypted=encrypt_token(token["access_token"]),
        refresh_token_encrypted=encrypt_token(token["refresh_token"])
        if token.get("refresh_token")
        else None,
        expires_at=expires,
    )
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
    mismatch = normalize_domain(data["counter_domain"]) != project.normalized_domain
    if mismatch and not data["confirm_domain_mismatch"]:
        messages.error(
            request, "Домен счётчика отличается от домена проекта. Подтвердите выбор явно."
        )
        return redirect("yandex:connection", project_id=project.id)
    YandexMetrikaProjectMapping.objects.update_or_create(
        project=project,
        defaults={
            "connection": connection_obj,
            "counter_id": data["counter_id"],
            "counter_name": data["counter_name"],
            "counter_domain": data["counter_domain"],
            "domain_mismatch_confirmed": mismatch,
            "selected_goals": [],
        },
    )
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
    available = list(MetrikaClient(mapping.connection).goals(mapping.counter_id))
    form = GoalsForm(request.POST, available_goals=available)
    if not form.is_valid():
        return HttpResponseBadRequest("Некорректные цели.")
    selected = set(form.cleaned_data["goals"])
    previous = {str(g["id"]): g for g in mapping.selected_goals}
    mapping.selected_goals = [
        {
            "id": str(g["id"]),
            "name": g.get("name", ""),
            "label": previous.get(str(g["id"]), {}).get("label", g.get("name", "")),
        }
        for g in available
        if str(g["id"]) in selected
    ]
    mapping.save(update_fields=["selected_goals", "updated_at"])
    return redirect("yandex:connection", project_id=project_id)


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
        return HttpResponseBadRequest("Некорректный месяц.")
    run = sync_metrika(mapping=mapping, report_month=form.cleaned_data["month"], user=request.user)
    if run.status == run.Status.SUCCESS:
        from apps.reports.models import Report

        report, _ = Report.objects.get_or_create(
            project=mapping.project, report_month=form.cleaned_data["month"].replace(day=1)
        )
        return redirect("reports:report-detail", report_id=report.id)
    messages.error(request, run.error_message)
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
