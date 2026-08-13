import logging
import secrets
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Count, Max, OuterRef, Subquery
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.models import Project

from .exporting import ExportBlocked, generate_artifact
from .forms import NarrativeEditForm, ReportCreateForm
from .models import GeneratedArtifact, NarrativeBlock, Report, ReportVersion, ValidationIssue
from .narratives import SECTION_ORDER
from .services import create_report_version
from .validation import validate_report_version

SECTION_TITLES = {
    "visibility": "Видимость",
    "position_distribution": "Распределение позиций",
    "top_10": "TOP-10",
    "top_11_20": "Запросы в TOP-11–20",
    "position_dynamics": "Динамика позиций",
    "traffic": "Трафик",
    "traffic_sources": "Источники трафика",
    "clicks_impressions": "Клики и показы",
    "ctr": "CTR",
    "indexing": "Индексация",
    "iks": "ИКС",
    "completed_work": "Выполненные работы",
}
ENGINE_LABELS = {"google": "Google", "yandex": "Яндекс"}
METRIC_SECTIONS = {
    "traffic": (
        "yandex_metrika",
        ("visits", "users", "new_users", "bounce_rate", "page_depth", "avg_visit_duration_seconds"),
    ),
    "clicks_impressions": ("yandex_webmaster", ("search_clicks", "search_impressions")),
    "ctr": ("yandex_webmaster", ("search_ctr",)),
    "indexing": ("yandex_webmaster", ("indexed_pages",)),
    "iks": ("yandex_webmaster", ("iks",)),
}
METRIC_LABELS = {
    "visits": "Визиты",
    "users": "Посетители",
    "new_users": "Новые посетители",
    "bounce_rate": "Показатель отказов",
    "page_depth": "Глубина просмотра",
    "avg_visit_duration_seconds": "Средняя длительность визита",
    "search_clicks": "Клики",
    "search_impressions": "Показы",
    "search_ctr": "CTR",
    "indexed_pages": "Проиндексированные страницы",
    "iks": "ИКС",
}
logger = logging.getLogger(__name__)


@login_required
def home(request):
    return redirect("reports:projects")


@login_required
def project_list(request):
    latest_month = (
        Report.objects.filter(project=OuterRef("pk"))
        .order_by("-report_month")
        .values("report_month")[:1]
    )
    projects = Project.objects.annotate(
        report_count=Count("reports", distinct=True), latest_report=Subquery(latest_month)
    )
    return render(request, "reports/project_list.html", {"projects": projects})


@login_required
def report_list(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    reports = project.reports.annotate(
        version_count=Count("versions"), latest_version_at=Max("versions__created_at")
    ).prefetch_related("versions__validation_issues")
    for report in reports:
        latest = max(report.versions.all(), key=lambda item: item.number, default=None)
        report.latest_version = latest
        report.ready = latest and not any(
            i.severity == ValidationIssue.Severity.ERROR for i in latest.validation_issues.all()
        )
    token = secrets.token_urlsafe(24)
    request.session[f"report_create_token:{project.id}"] = token
    form = ReportCreateForm(project=project, initial={"submission_token": token})
    calendar_fields = [
        (engine, label, form[f"{engine}_dates"])
        for engine, label in (("yandex", "Яндекс"), ("google", "Google"))
        if engine in form.connected_engines
    ]
    return render(
        request,
        "reports/report_list.html",
        {"project": project, "reports": reports, "form": form, "calendar_fields": calendar_fields},
    )


@login_required
def report_create(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    form = ReportCreateForm(request.POST, project=project)
    if form.is_valid():
        submitted_token = form.cleaned_data.get("submission_token")
        token_key = f"report_create_token:{project.id}"
        if submitted_token and submitted_token != request.session.pop(token_key, None):
            messages.warning(request, "Запрос уже обработан. Новая версия не создана.")
            return redirect("reports:report-list", project_id=project.id)
        selected_by_engine = {
            engine: form.cleaned_data[f"{engine}_dates"] for engine in ("yandex", "google")
        }
        selected_dates = sorted({day for dates in selected_by_engine.values() for day in dates})
        selected_source_ids = (
            form.cleaned_data["metrika_snapshots"] + form.cleaned_data["webmaster_snapshots"]
        )
        from apps.metrics.models import SourceSnapshot

        source_rows = SourceSnapshot.objects.filter(id__in=selected_source_ids)
        endpoint = (
            date.fromisoformat(selected_dates[-1])
            if selected_dates
            else max((item.period_end for item in source_rows), default=timezone.localdate())
        )
        month = form.cleaned_data.get("month") or endpoint.replace(day=1)
        report, created = Report.objects.get_or_create(project=project, report_month=month)
        version = create_report_version(
            report=report,
            created_by=request.user,
            selection={
                "topvisor": selected_by_engine,
                "display_options": {"show_urls": form.cleaned_data["show_urls"]},
                "yandex_metrika": form.cleaned_data["metrika_snapshots"],
                "yandex_webmaster": form.cleaned_data["webmaster_snapshots"],
            },
        )
        validate_report_version(version)
        messages.info(
            request,
            "Отчёт и версия созданы."
            if created
            else "Для существующего отчёта создана новая неизменяемая версия.",
        )
        return redirect("reports:report-detail", report_id=report.id)
    reports = project.reports.annotate(
        version_count=Count("versions"), latest_version_at=Max("versions__created_at")
    )
    calendar_fields = [
        (engine, label, form[f"{engine}_dates"])
        for engine, label in (("yandex", "Яндекс"), ("google", "Google"))
        if engine in form.connected_engines
    ]
    return render(
        request,
        "reports/report_list.html",
        {"project": project, "reports": reports, "form": form, "calendar_fields": calendar_fields},
        status=400,
    )


@login_required
def report_detail(request, report_id):
    report = get_object_or_404(Report.objects.select_related("project"), pk=report_id)
    token = secrets.token_urlsafe(24)
    request.session[f"version_token:{report.id}"] = token
    versions = report.versions.select_related("created_by", "snapshot").prefetch_related(
        "validation_issues"
    )
    for version in versions:
        version.error_count = sum(
            issue.severity == ValidationIssue.Severity.ERROR
            for issue in version.validation_issues.all()
        )
        version.warning_count = sum(
            issue.severity == ValidationIssue.Severity.WARNING
            for issue in version.validation_issues.all()
        )
    return render(
        request,
        "reports/report_detail.html",
        {
            "project": report.project,
            "report": report,
            "versions": versions,
            "version_token": token,
        },
    )


@login_required
def version_create(request, report_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    report = get_object_or_404(Report, pk=report_id)
    key = f"version_token:{report.id}"
    if not request.POST.get("token") or request.POST["token"] != request.session.pop(key, None):
        messages.warning(request, "Запрос уже обработан. Новая версия не создана.")
        return redirect("reports:report-detail", report_id=report.id)
    try:
        version = create_report_version(report=report, created_by=request.user)
        validate_report_version(version)
    except IntegrityError:
        messages.error(request, "Не удалось создать дублирующую версию.")
        return redirect("reports:report-detail", report_id=report.id)
    return redirect("reports:version-detail", version_id=version.id)


def _segment_rows(payload, code):
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    rows = []
    for source in segments:
        row = dict(source)
        row["engine_label"] = ENGINE_LABELS.get(
            source.get("search_engine"), source.get("search_engine") or "Поиск"
        )
        if code == "top_11_20":
            row["rows"] = source.get("top_11_20", [])
        rows.append(row)
    return rows


def _metric_rows(payload, code):
    if code not in METRIC_SECTIONS:
        return []
    source, codes = METRIC_SECTIONS[code]
    facts = payload.get("calculated", {}).get("sources", {}).get("sources", {}).get(source, {})
    changes = facts.get("normalized_changes", {})
    series = facts.get("three_month_series", {})
    if code == "traffic":
        codes = (*codes, *(key for key in sorted(series) if key.startswith("goal_")))
    return [
        {
            "code": metric_code,
            "label": METRIC_LABELS.get(metric_code, metric_code),
            "change": changes.get(metric_code, {}),
            "series": series.get(metric_code, []),
        }
        for metric_code in codes
        if metric_code in changes or metric_code in series
    ]


def _traffic_rows(payload):
    traffic = (
        payload.get("calculated", {})
        .get("sources", {})
        .get("sources", {})
        .get("yandex_metrika", {})
        .get("traffic_sources", {})
    )
    values = {}
    report_start = payload.get("periods", {}).get("report", {}).get("start")
    for source in payload.get("source_snapshots", []):
        if source.get("source") != "yandex_metrika" or source.get("period_start") != report_start:
            continue
        for metric in source.get("metrics", []):
            code = metric.get("code", "")
            if code.startswith("source_"):
                values[code.removeprefix("source_").removesuffix("_visits")] = metric.get("value")
    return [
        {"source": source, "visits": values.get(source), "share": share}
        for source, share in traffic.get("shares", {}).items()
    ]


def _preview_context(version, *, form_overrides=None):
    snapshot = version.snapshot
    blocks = {block.section_code: block for block in version.narrative_blocks.all()}
    issues = list(version.validation_issues.all())
    sections = []
    payload = snapshot.payload
    show_urls = payload.get("display_options", {}).get("show_urls", True)
    form_overrides = form_overrides or {}
    for code in SECTION_ORDER:
        block = blocks.get(code)
        if not block:
            continue
        section_issues = [issue for issue in issues if issue.section_code == code]
        sections.append(
            {
                "code": code,
                "title": SECTION_TITLES.get(code, code),
                "block": block,
                "facts": block.facts,
                "issues": section_issues,
                "form": form_overrides.get(block.id, NarrativeEditForm(instance=block)),
                "segments": _segment_rows(payload, code),
                "metric_rows": _metric_rows(payload, code),
                "traffic_rows": _traffic_rows(payload) if code == "traffic_sources" else [],
                "work_rows": payload.get("completed_work", []) if code == "completed_work" else [],
                "periods": payload.get("periods", {}),
                "show_urls": show_urls,
            }
        )
    provenance = []
    for item in payload.get("ranking_sources", []):
        source = item.get("provenance", {})
        provenance.append(
            {
                "source": item.get("search_engine"),
                "region": item.get("region"),
                "method": source.get("method"),
                "period": item.get("date"),
                "retrieved_at": source.get("retrieved_at"),
                "checksum": source.get("response_checksum"),
                "identifier": source.get("import_batch_id") or item.get("id"),
            }
        )
    for item in payload.get("source_snapshots", []):
        source = item.get("provenance", {})
        provenance.append(
            {
                "source": item.get("source"),
                "method": source.get("method"),
                "period": f"{item.get('period_start')} — {item.get('period_end')}",
                "retrieved_at": item.get("retrieved_at"),
                "checksum": item.get("checksum"),
                "identifier": item.get("id"),
            }
        )
    errors = sum(i.severity == ValidationIssue.Severity.ERROR for i in issues)
    warnings = sum(i.severity == ValidationIssue.Severity.WARNING for i in issues)
    return {
        "version": version,
        "report": version.report,
        "snapshot_project": payload.get("project", {}),
        "snapshot_month": payload.get("periods", {}).get("report", {}).get("start"),
        "sections": sections,
        "issues": issues,
        "errors": errors,
        "warnings": warnings,
        "can_publish": not errors,
        "provenance": provenance,
        "show_urls": show_urls,
    }


@login_required
def version_detail(request, version_id):
    version = get_object_or_404(
        ReportVersion.objects.select_related(
            "report__project", "snapshot", "created_by"
        ).prefetch_related("narrative_blocks", "validation_issues"),
        pk=version_id,
    )
    context = _preview_context(version)
    context["project"] = version.report.project
    stale_before = timezone.now() - timedelta(seconds=settings.REPORT_ARTIFACT_STALE_SECONDS)
    version.generated_artifacts.filter(
        status=GeneratedArtifact.Status.GENERATING, created_at__lt=stale_before
    ).update(
        status=GeneratedArtifact.Status.FAILED,
        generation_log="Формирование было прервано и не завершилось вовремя.",
    )
    context["artifacts"] = version.generated_artifacts.all()
    return render(request, "reports/version_detail.html", context)


@login_required
def artifact_generate(request, version_id, artifact_type):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    version = get_object_or_404(ReportVersion, pk=version_id)
    if artifact_type not in GeneratedArtifact.Type.values:
        raise Http404
    try:
        generate_artifact(
            version=version,
            artifact_type=artifact_type,
            is_draft=request.POST.get("is_draft") == "on",
            created_by=request.user,
        )
        messages.success(request, f"Файл {artifact_type.upper()} сформирован.")
    except ExportBlocked as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        logger.exception("Artifact generation failed: %s", type(exc).__name__)
        messages.error(request, "Не удалось сформировать файл; безопасный журнал сохранён.")
    return redirect("reports:version-detail", version_id=version.id)


@login_required
def artifact_download(request, artifact_id):
    artifact = get_object_or_404(GeneratedArtifact, pk=artifact_id, status="ready")
    expected = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    if artifact.mime_type != expected.get(artifact.artifact_type) or not artifact.filename.endswith(
        "." + artifact.artifact_type
    ):
        raise Http404
    return FileResponse(
        artifact.file.open("rb"),
        as_attachment=True,
        filename=artifact.filename,
        content_type=artifact.mime_type,
    )


@login_required
@require_POST
def artifact_delete(request, artifact_id):
    artifact = get_object_or_404(
        GeneratedArtifact.objects.select_related("report_version__report__project"), pk=artifact_id
    )
    version_id = artifact.report_version_id
    stale = (
        artifact.status == GeneratedArtifact.Status.GENERATING
        and artifact.created_at
        < timezone.now() - timedelta(seconds=settings.REPORT_ARTIFACT_STALE_SECONDS)
    )
    if artifact.status == GeneratedArtifact.Status.GENERATING and not stale:
        messages.error(request, "Нельзя удалить файл, пока он формируется.")
        return redirect("reports:version-detail", version_id=version_id)
    if artifact.file:
        artifact.file.delete(save=False)
    artifact.delete()
    messages.success(request, "Созданный файл удалён.")
    return redirect("reports:version-detail", version_id=version_id)


def _updated_preview(request, version):
    version = (
        ReportVersion.objects.select_related("report__project", "snapshot", "created_by")
        .prefetch_related("narrative_blocks", "validation_issues")
        .get(pk=version.pk)
    )
    if request.headers.get("HX-Request"):
        return render(request, "reports/_preview.html", _preview_context(version))
    return redirect("reports:version-detail", version_id=version.id)


@login_required
def version_validate(request, version_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    version = get_object_or_404(ReportVersion, pk=version_id)
    validate_report_version(version)
    return _updated_preview(request, version)


def _block(block_id):
    return get_object_or_404(
        NarrativeBlock.objects.select_related("report_version__report__project"), pk=block_id
    )


@login_required
def narrative_edit(request, block_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    block = _block(block_id)
    form = NarrativeEditForm(request.POST, instance=block)
    if form.is_valid():
        block = form.save(commit=False)
        block.status = (
            NarrativeBlock.Status.EDITED
            if block.edited_text.strip()
            else NarrativeBlock.Status.GENERATED
        )
        block.confirmed_by = None
        block.confirmed_at = None
        block.save()
        validate_report_version(block.report_version)
    else:
        version = (
            ReportVersion.objects.select_related("report__project", "snapshot", "created_by")
            .prefetch_related("narrative_blocks", "validation_issues")
            .get(pk=block.report_version_id)
        )
        return render(
            request,
            "reports/_preview.html",
            _preview_context(version, form_overrides={block.id: form}),
        )
    return _updated_preview(request, block.report_version)


@login_required
def narrative_reset(request, block_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    block = _block(block_id)
    block.edited_text = ""
    block.status = NarrativeBlock.Status.GENERATED
    block.confirmed_by = None
    block.confirmed_at = None
    block.save()
    validate_report_version(block.report_version)
    return _updated_preview(request, block.report_version)


@login_required
def narrative_confirm(request, block_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    block = _block(block_id)
    block.status = NarrativeBlock.Status.CONFIRMED
    block.confirmed_by = request.user
    block.confirmed_at = timezone.now()
    block.save()
    validate_report_version(block.report_version)
    return _updated_preview(request, block.report_version)
