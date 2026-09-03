from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project
from apps.yandex.crypto import CredentialConfigurationError

from .client import SerphuntClient, SerphuntError
from .forms import SerphuntCredentialsForm, SerphuntProjectForm
from .models import SerphuntCredential, SerphuntProjectMapping
from .services import sync_positions


@staff_member_required
def credentials(request):
    credential = SerphuntCredential.objects.filter(pk=1).first()
    has_key = credential is not None
    if request.method == "POST" and request.POST.get("action") == "delete":
        with transaction.atomic():
            SerphuntProjectMapping.objects.all().delete()
            SerphuntCredential.objects.all().delete()
        messages.success(request, "Реквизиты Serphunt удалены.")
        return redirect("serphunt:credentials")
    form = SerphuntCredentialsForm(request.POST or None, has_key=has_key)
    if request.method == "POST" and form.is_valid():
        submitted = form.cleaned_data["api_key"]
        try:
            current = credential.get_api_key() if credential and not submitted else submitted
            balance = SerphuntClient(current).balance()
            replacement = credential or SerphuntCredential()
            replacement.set_api_key(current)
            replacement.last_verified_at = timezone.now()
            replacement.save()
            messages.success(
                request, f"API-ключ Serphunt проверен. Баланс: {balance.get('balance', '—')}."
            )
            return redirect("serphunt:credentials")
        except CredentialConfigurationError:
            form.add_error(
                None,
                "Не удалось прочитать или зашифровать реквизиты. Проверьте ключ "
                "шифрования либо введите API-ключ заново.",
            )
        except SerphuntError as exc:
            form.add_error(None, str(exc))
    return render(request, "serphunt/credentials.html", {"credential": credential, "form": form})


@login_required
def connection(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    mapping = SerphuntProjectMapping.objects.filter(project=project).first()
    form = SerphuntProjectForm(request.POST or None, instance=mapping)
    if request.method == "POST" and form.is_valid():
        mapping = form.save(commit=False)
        mapping.project = project
        mapping.save()
        project.position_provider = Project.PositionProvider.SERPHUNT
        project.save(update_fields=["position_provider", "updated_at"])
        messages.success(request, "Настройки Serphunt сохранены; сервис выбран для позиций.")
        return redirect("serphunt:connection", project_id=project.id)
    return render(
        request,
        "serphunt/connection.html",
        {
            "project": project,
            "mapping": mapping,
            "credential": SerphuntCredential.objects.filter(pk=1).first(),
            "form": form,
            "runs": mapping.sync_runs.all()[:10] if mapping else (),
        },
    )


@login_required
def sync(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    mapping = get_object_or_404(SerphuntProjectMapping, project=project)
    try:
        run = sync_positions(mapping)
    except (CredentialConfigurationError, SerphuntError) as exc:
        run = None
        message = (
            "Не удалось прочитать реквизиты Serphunt."
            if isinstance(exc, CredentialConfigurationError)
            else str(exc)
        )
    else:
        message = (
            f"Serphunt: загружено позиций — {run.loaded_keyword_count}."
            if run.status == run.Status.SUCCESS
            else run.error_message or "Задание Serphunt ещё выполняется."
        )
    ok = bool(run and run.status == run.Status.SUCCESS)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": ok, "message": message}, status=200 if ok else 502)
    (messages.success if ok else messages.error)(request, message)
    return redirect("serphunt:connection", project_id=project.id)
