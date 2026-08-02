import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Count, Max, OuterRef, Subquery
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.projects.models import Project

from .forms import NarrativeEditForm, ReportCreateForm
from .models import NarrativeBlock, Report, ReportVersion, ValidationIssue
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
    return render(
        request,
        "reports/report_list.html",
        {"project": project, "reports": reports, "form": ReportCreateForm()},
    )


@login_required
def report_create(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    form = ReportCreateForm(request.POST)
    if form.is_valid():
        report, created = Report.objects.get_or_create(
            project=project, report_month=form.cleaned_data["month"]
        )
        messages.info(
            request,
            "Отчёт создан."
            if created
            else "Отчёт за этот месяц уже существует; открыт существующий отчёт.",
        )
        return redirect("reports:report-detail", report_id=report.id)
    reports = project.reports.annotate(
        version_count=Count("versions"), latest_version_at=Max("versions__created_at")
    )
    return render(
        request,
        "reports/report_list.html",
        {"project": project, "reports": reports, "form": form},
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
        {"report": report, "versions": versions, "version_token": token},
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


def _preview_context(version):
    snapshot = version.snapshot
    blocks = {block.section_code: block for block in version.narrative_blocks.all()}
    issues = list(version.validation_issues.all())
    sections = []
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
                "form": NarrativeEditForm(instance=block),
            }
        )
    payload = snapshot.payload
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
                "retrieved_at": source.get("generated_at"),
                "checksum": source.get("checksum"),
                "identifier": item.get("id"),
            }
        )
    errors = sum(i.severity == ValidationIssue.Severity.ERROR for i in issues)
    warnings = sum(i.severity == ValidationIssue.Severity.WARNING for i in issues)
    return {
        "version": version,
        "report": version.report,
        "sections": sections,
        "issues": issues,
        "errors": errors,
        "warnings": warnings,
        "can_publish": not errors,
        "provenance": provenance,
    }


@login_required
def version_detail(request, version_id):
    version = get_object_or_404(
        ReportVersion.objects.select_related(
            "report__project", "snapshot", "created_by"
        ).prefetch_related("narrative_blocks", "validation_issues"),
        pk=version_id,
    )
    return render(request, "reports/version_detail.html", _preview_context(version))


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
        form.save()
        validate_report_version(block.report_version)
    else:
        return render(
            request, "reports/_narrative_form.html", {"block": block, "form": form}, status=400
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
