import base64
import calendar
import json
import logging
import secrets
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Count, Max, OuterRef, Subquery
from django.http import (
    FileResponse,
    Http404,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.models import Project
from apps.topvisor.models import TopvisorProjectMapping

from .exporting import ExportBlocked, generate_artifact
from .forms import (
    BOOLEAN_REPORT_FIELDS,
    PERSISTED_REPORT_FIELDS,
    NarrativeEditForm,
    ReportCreateForm,
    parse_named_url_groups,
)
from .models import (
    GeneratedArtifact,
    NarrativeBlock,
    ProjectReportSettings,
    Report,
    ReportVersion,
    ValidationIssue,
)
from .narratives import SECTION_ORDER, TOP_SECTION_RANGES, section_enabled
from .rich_text import sanitize_report_html
from .services import ReportVersionDeleteBlocked, create_report_version, delete_report_version
from .validation import validate_report_version

SECTION_TITLES = {
    "visibility": "Видимость",
    "position_distribution": "Распределение позиций",
    "top_5": "Запросы в TOP-5",
    "top_10": "Запросы в TOP-10",
    "top_20": "Запросы в TOP-20",
    "top_11_30": "Запросы в TOP-11–30",
    "top_30": "Запросы в TOP-30",
    "top_11_20": "Запросы в TOP-11–20",
    "position_dynamics": "Динамика позиций по месяцам",
    "traffic": "Трафик",
    "traffic_sources": "Источники трафика",
    "clicks_impressions": "Клики и показы",
    "ctr": "CTR",
    "indexing": "Индексация",
    "iks": "ИКС",
    "webmaster_popular_queries": "Самые кликабельные запросы",
    "geography": "География посетителей",
    "metrika_search_engines": "Поисковые системы",
    "metrika_landing_pages": "Популярные страницы входа",
    "metrika_landing_page_comparison": "Страницы входа: Яндекс и Google",
    "metrika_url_groups": "Информационные и коммерческие страницы",
    "metrika_sections": "Данные по разделам",
    "metrika_categories": "Прорабатываемые категории",
    "metrika_goals": "Цели Метрики",
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
    "iks": ("yandex_webmaster", ("iks", "quality_index")),
    "geography": (
        "yandex_metrika",
        (
            "geography_moscow_visits",
            "geography_saint_petersburg_visits",
        ),
    ),
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
    "geography_moscow_visits": "Москва",
    "geography_saint_petersburg_visits": "Санкт-Петербург",
    "geography_undefined_visits": "Не определено",
    "geography_area_undefined_visits": "Область не определена",
}
logger = logging.getLogger(__name__)


def _persisted_report_values(form):
    values = {
        name: form.cleaned_data.get(name)
        for name in PERSISTED_REPORT_FIELDS
        if name in form.cleaned_data
    }
    values["topvisor_report_urls"] = form.cleaned_topvisor_report_urls()
    return values


def _validated_manual_rows(value):
    try:
        rows = json.loads(value or "[]") if isinstance(value, str) else value
    except json.JSONDecodeError:
        raise ValidationError("Некорректные ручные значения Topvisor.") from None
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValidationError("Некорректные ручные значения Topvisor.")
    allowed = {
        "configuration_id",
        "engine",
        "region",
        "month",
        "visibility",
        "total",
        "top3",
        "top10",
        "top11_30",
    }
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaned.append({key: row.get(key) for key in allowed if key in row})
    return cleaned


def _topvisor_editor_rows(project):
    """Latest snapshot in every configuration/month for the editable report table."""
    from apps.metrics.models import RankingSnapshot

    snapshots = (
        RankingSnapshot.objects.filter(
            project=project,
            snapshot_date__gte=timezone.localdate() - timedelta(days=550),
        )
        .prefetch_related("positions")
        .order_by("snapshot_date", "created_at", "id")
    )
    latest = {}
    for snapshot in snapshots:
        key = (
            snapshot.search_engine,
            snapshot.region,
            snapshot.topvisor_configuration_id,
            snapshot.snapshot_date.replace(day=1),
        )
        latest[key] = snapshot
    rows = []
    for (engine, region, configuration, month), snapshot in sorted(latest.items()):
        positions = list(snapshot.positions.all())
        ranked = [row.position_value for row in positions if row.position_value is not None]
        rows.append(
            {
                "configuration_id": configuration,
                "engine": engine,
                "region": region,
                "month": month.isoformat(),
                "visibility": float(snapshot.visibility) if snapshot.visibility is not None else 0,
                "total": len(positions),
                "top3": sum(value <= 3 for value in ranked),
                "top10": sum(value <= 10 for value in ranked),
                "top11_30": sum(11 <= value <= min(snapshot.ranking_depth, 30) for value in ranked),
            }
        )
    return rows


def _url_segment_settings(cleaned):
    return {
        "information": parse_named_url_groups(cleaned.get("metrika_info_url_groups")),
        "commercial": parse_named_url_groups(cleaned.get("metrika_commercial_url_groups")),
        "categories": parse_named_url_groups(cleaned.get("metrika_category_url_groups")),
    }


def _calendar_months(field, count=3):
    """Return an initial, fully rendered calendar; JavaScript only enhances it."""
    available = {str(value) for value, _label in field.field.choices}
    if not available:
        return []
    selected = set(field.value() or [])
    latest = date.fromisoformat(max(available)).replace(day=1)
    start_index = latest.year * 12 + latest.month - count
    months = []
    month_names = (
        "Январь",
        "Февраль",
        "Март",
        "Апрель",
        "Май",
        "Июнь",
        "Июль",
        "Август",
        "Сентябрь",
        "Октябрь",
        "Ноябрь",
        "Декабрь",
    )
    for offset in range(count):
        index = start_index + offset
        year, zero_month = divmod(index, 12)
        month = zero_month + 1
        weeks = []
        for week in calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(year, month):
            weeks.append(
                [
                    {
                        "iso": day.isoformat(),
                        "number": day.day,
                        "in_month": day.month == month,
                        "available": day.isoformat() in available and day.month == month,
                        "selected": day.isoformat() in selected and day.month == month,
                    }
                    for day in week
                ]
            )
        months.append(
            {
                "year": year,
                "month": month,
                "title": f"{month_names[month - 1]} {year}",
                "weeks": weeks,
            }
        )
    return months


def _calendar_period(months):
    if not months:
        return ""
    first, last = months[0], months[-1]
    first_name = first["title"].rsplit(" ", 1)[0]
    if first["year"] == last["year"]:
        return f"{first_name} — {last['title']}"
    return f"{first['title']} — {last['title']}"


def _calendar_fields(form):
    fields = []
    for engine, label in (("yandex", "Яндекс"), ("google", "Google")):
        if engine not in form.connected_engines:
            continue
        field = form[f"{engine}_dates"]
        months = _calendar_months(field)
        fields.append((engine, label, field, months, _calendar_period(months)))
    return fields


def _source_period_fields(form):
    def period_word(count):
        if count % 10 == 1 and count % 100 != 11:
            return "период"
        if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            return "периода"
        return "периодов"

    fields = []
    for (
        name,
        label,
        short_label,
        description,
        unavailable_description,
        mapping_relation,
        sync_route,
    ) in (
        (
            "metrika_snapshots",
            "Яндекс.Метрика",
            "Метрика",
            "Периоды для разделов «Трафик» и «Источники трафика».",
            "разделы «Трафик» и «Источники трафика» не будут заполнены.",
            "yandex_metrika_mapping",
            "yandex:sync",
        ),
        (
            "webmaster_snapshots",
            "Яндекс.Вебмастер",
            "Вебмастер",
            "Периоды для кликов, показов, CTR, индексации и ИКС.",
            "клики, показы, CTR, индексация и ИКС не будут заполнены.",
            "yandex_webmaster_mapping",
            "yandex:sync-webmaster",
        ),
    ):
        bound = form[name]
        selected = {str(value) for value in (bound.value() or [])}
        options = [
            {**option, "selected": option["id"] in selected}
            for option in form.source_period_options.get(name, [])
        ]
        selected_months = [option["month"] for option in options if option["selected"]]
        mapping = getattr(form.project, mapping_relation, None)
        end = max(selected_months, default="")
        sync_month = end or getattr(form, "report_month", timezone.localdate()).strftime("%Y-%m")
        fields.append(
            {
                "name": name,
                "source_code": (
                    "yandex_metrika" if name == "metrika_snapshots" else "yandex_webmaster"
                ),
                "label": label,
                "short_label": short_label,
                "description": description,
                "unavailable_description": unavailable_description,
                "options": options,
                "selected_count": len(selected_months),
                "period_word": period_word(len(selected_months)),
                "start": min(selected_months, default=""),
                "end": end,
                "errors": bound.errors,
                "connected": mapping is not None,
                "last_synced_at": getattr(mapping, "last_successful_sync_at", None),
                "sync_form_id": f"sync-{name.replace('_snapshots', '')}-form",
                "cleanup_form_id": f"cleanup-{name.replace('_snapshots', '')}-form",
                "sync_url": reverse(sync_route, args=[form.project.id]),
                "sync_month": sync_month,
            }
        )
    return fields


def _topvisor_report_link_fields(form):
    return [{**item, "field": form[item["name"]]} for item in form.topvisor_report_link_fields]


def _metrika_goal_context(project, user):
    from apps.yandex.client import MetrikaClient, YandexAPIError
    from apps.yandex.crypto import CredentialConfigurationError
    from apps.yandex.models import YandexMetrikaProjectMapping

    mapping = (
        YandexMetrikaProjectMapping.objects.filter(
            project=project, connection__user=user, connection__active=True
        )
        .select_related("connection")
        .first()
    )
    if not mapping:
        return {"metrika_mapping": None, "metrika_goal_options": [], "goal_picker_error": ""}
    error = ""
    try:
        available = list(MetrikaClient(mapping.connection).goals(mapping.counter_id))
    except (YandexAPIError, CredentialConfigurationError):
        available = mapping.selected_goals
        error = "Не удалось обновить список целей. Показаны ранее выбранные цели."
    selected = {str(goal.get("id")) for goal in mapping.selected_goals}
    options = [
        {
            "id": str(goal.get("id", "")),
            "label": str(goal.get("name") or goal.get("label") or f"Цель {goal.get('id', '')}"),
            "selected": str(goal.get("id", "")) in selected,
        }
        for goal in available
    ]
    return {
        "metrika_mapping": mapping,
        "metrika_goal_options": options,
        "selected_goal_count": sum(option["selected"] for option in options),
        "goal_picker_error": error,
    }


def _topvisor_report_url(payload, source):
    options = payload.get("display_options", {})
    if not options.get("include_topvisor_report_link"):
        return ""
    urls = options.get("topvisor_report_urls") or {}
    configuration = str(
        source.get("configuration_id") or source.get("topvisor_configuration_id") or ""
    )
    if urls:
        return urls.get(configuration, "")
    return options.get("topvisor_report_url", "")


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
    calendar_fields = _calendar_fields(form)
    source_period_fields = _source_period_fields(form)
    topvisor_report_link_fields = _topvisor_report_link_fields(form)
    can_create = all(
        len(form.fields[f"{engine}_dates"].choices) >= 2 for engine in form.connected_engines
    )
    context = {
        "project": project,
        "reports": reports,
        "form": form,
        "calendar_fields": calendar_fields,
        "source_period_fields": source_period_fields,
        "topvisor_report_link_fields": topvisor_report_link_fields,
        "can_create": can_create,
        "topvisor_sync_url": reverse("topvisor:sync", args=[project.id])
        if TopvisorProjectMapping.objects.filter(project=project).exists()
        else "",
        "topvisor_editor_rows": _topvisor_editor_rows(project),
    }
    context.update(_metrika_goal_context(project, request.user))
    return render(
        request,
        "reports/report_list.html",
        context,
    )


@login_required
def report_create(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = get_object_or_404(Project, pk=project_id)
    form = ReportCreateForm(request.POST, request.FILES, project=project)
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

        source_rows = SourceSnapshot.objects.filter(project=project, id__in=selected_source_ids)
        endpoint = (
            date.fromisoformat(selected_dates[-1])
            if selected_dates
            else max((item.period_end for item in source_rows), default=timezone.localdate())
        )
        month = form.cleaned_data.get("month") or endpoint.replace(day=1)
        report, created = Report.objects.get_or_create(project=project, report_month=month)
        screenshot = form.cleaned_data.get("webmaster_queries_screenshot")
        screenshot_payload = None
        if screenshot:
            screenshot.seek(0)
            screenshot_payload = {
                "name": screenshot.name,
                "mime_type": screenshot.content_type,
                "data": base64.b64encode(screenshot.read()).decode("ascii"),
            }
        topvisor_report_urls = form.cleaned_topvisor_report_urls()
        persisted_values = _persisted_report_values(form)
        ProjectReportSettings.objects.update_or_create(
            project=project, defaults={"values": persisted_values}
        )
        manual_rows = _validated_manual_rows(form.cleaned_data.get("topvisor_manual_rows"))
        version = create_report_version(
            report=report,
            created_by=request.user,
            selection={
                "topvisor": selected_by_engine,
                "display_options": {
                    "configuration_version": 3,
                    **{
                        name: form.cleaned_data[name]
                        for name in (
                            "show_urls",
                            "include_visibility",
                            "include_monthly_dynamics",
                            "include_top_tables",
                            "include_top_5",
                            "include_top_10",
                            "include_top_20",
                            "include_top_11_30",
                            "include_top_30",
                            "include_topvisor_report_link",
                            "include_webmaster",
                            "webmaster_chart_period",
                            "include_webmaster_popular_queries",
                            "include_metrika",
                            "metrika_robotness",
                            "metrika_search_segment",
                            "include_metrika_sources_table",
                            "include_metrika_search_engines",
                            "metrika_bar_search_engines",
                            "include_metrika_geography",
                            "geography_moscow",
                            "geography_moscow_region",
                            "geography_saint_petersburg",
                            "geography_saint_petersburg_region",
                            "geography_undefined",
                            "geography_area_undefined",
                            "include_metrika_landing_pages",
                            "include_metrika_landing_page_comparison",
                            "include_metrika_url_groups",
                            "include_metrika_sections",
                            "include_metrika_categories",
                            "metrika_categories_combined",
                            "include_metrika_goals",
                            "metrika_goals_quarter",
                            "include_completed_work",
                            "completed_work_text",
                        )
                    },
                    "metrika_url_segments": _url_segment_settings(form.cleaned_data),
                    "topvisor_manual_rows": manual_rows,
                    "topvisor_report_urls": topvisor_report_urls,
                    "topvisor_report_url": (
                        next(iter(topvisor_report_urls.values()), "")
                        or form.cleaned_data["topvisor_report_url"]
                    ),
                    "webmaster_queries_comment": form.cleaned_data["webmaster_queries_comment"],
                    "webmaster_queries_screenshot": screenshot_payload,
                },
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
    calendar_fields = _calendar_fields(form)
    source_period_fields = _source_period_fields(form)
    topvisor_report_link_fields = _topvisor_report_link_fields(form)
    can_create = all(
        len(form.fields[f"{engine}_dates"].choices) >= 2 for engine in form.connected_engines
    )
    context = {
        "project": project,
        "reports": reports,
        "form": form,
        "calendar_fields": calendar_fields,
        "source_period_fields": source_period_fields,
        "topvisor_report_link_fields": topvisor_report_link_fields,
        "can_create": can_create,
        "topvisor_sync_url": reverse("topvisor:sync", args=[project.id])
        if TopvisorProjectMapping.objects.filter(project=project).exists()
        else "",
        "topvisor_editor_rows": _topvisor_editor_rows(project),
    }
    context.update(_metrika_goal_context(project, request.user))
    return render(
        request,
        "reports/report_list.html",
        context,
        status=400,
    )


@login_required
@require_POST
def report_settings_save(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    try:
        incoming = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "message": "Некорректные настройки."}, status=400)
    if not isinstance(incoming, dict):
        return JsonResponse({"ok": False, "message": "Некорректные настройки."}, status=400)
    previous = ProjectReportSettings.objects.filter(project=project).only("values").first()
    previous_values = (previous.values if previous else {}) or {}
    old_retention = previous_values.get("sync_log_retention_months")
    values = dict(previous_values)
    for name in PERSISTED_REPORT_FIELDS:
        if name not in incoming:
            continue
        value = incoming[name]
        if name in BOOLEAN_REPORT_FIELDS:
            values[name] = bool(value)
        elif name == "metrika_bar_search_engines":
            allowed = {"google", "yandex", "bing", "yahoo"}
            values[name] = [str(item) for item in (value or []) if str(item) in allowed]
        elif name == "sync_log_retention_months":
            values[name] = str(value) if str(value) in {"6", "12", "forever"} else "12"
        elif name == "topvisor_manual_rows":
            try:
                values[name] = json.dumps(_validated_manual_rows(value), ensure_ascii=False)
            except ValidationError as exc:
                return JsonResponse({"ok": False, "message": "; ".join(exc.messages)}, status=400)
        else:
            values[name] = (
                sanitize_report_html(value)
                if name == "completed_work_text"
                else str(value or "")[:20_000]
            )
    for name in (
        "metrika_info_url_groups",
        "metrika_commercial_url_groups",
        "metrika_category_url_groups",
    ):
        try:
            parse_named_url_groups(values.get(name, ""))
        except ValidationError as exc:
            return JsonResponse({"ok": False, "message": "; ".join(exc.messages)}, status=400)
    raw_urls = incoming.get("topvisor_report_urls")
    if isinstance(raw_urls, dict):
        values["topvisor_report_urls"] = {
            str(key)[:200]: str(value)[:2000] for key, value in raw_urls.items() if value
        }
    ProjectReportSettings.objects.update_or_create(project=project, defaults={"values": values})
    retention = values.get("sync_log_retention_months", "12")
    if retention != old_retention:
        from apps.yandex.services import prune_sync_runs

        prune_sync_runs(project, None if retention == "forever" else int(retention))
    return JsonResponse({"ok": True, "message": "Настройки проекта сохранены."})


@login_required
@require_POST
def source_history_clear(request, project_id):
    from apps.metrics.models import SourceSnapshot
    from apps.yandex.models import (
        YandexMetrikaProjectMapping,
        YandexMetrikaSyncRun,
        YandexWebmasterProjectMapping,
        YandexWebmasterSyncRun,
    )

    project = get_object_or_404(Project, pk=project_id)
    source = request.POST.get("source")
    configuration = {
        SourceSnapshot.Source.METRIKA: (
            YandexMetrikaProjectMapping,
            YandexMetrikaSyncRun,
            "Метрики",
        ),
        SourceSnapshot.Source.WEBMASTER: (
            YandexWebmasterProjectMapping,
            YandexWebmasterSyncRun,
            "Вебмастера",
        ),
    }.get(source)
    if not configuration:
        return HttpResponseBadRequest("Некорректный источник.")
    mapping_model, run_model, label = configuration
    mapping = get_object_or_404(
        mapping_model,
        project=project,
        connection__user=request.user,
    )
    action = request.POST.get("action")
    snapshots = SourceSnapshot.objects.filter(project=project, source=source)
    if action == "delete_runs":
        deleted, _ = run_model.objects.filter(mapping=mapping).delete()
        messages.success(request, f"Журнал синхронизации {label} очищен: {deleted} записей.")
    elif action == "delete_all":
        deleted = snapshots.count()
        snapshots.delete()
        mapping.last_successful_sync_at = None
        mapping.save(update_fields=["last_successful_sync_at", "updated_at"])
        messages.success(request, f"Все сохранённые месяцы {label} удалены: {deleted} записей.")
    elif action == "delete_selected":
        try:
            months = {date.fromisoformat(f"{value}-01") for value in request.POST.getlist("months")}
        except ValueError:
            return HttpResponseBadRequest("Некорректный месяц.")
        if not months:
            messages.warning(request, "Выберите месяцы для удаления.")
        else:
            selected = snapshots.filter(period_start__in=months)
            deleted = selected.count()
            selected.delete()
            if not snapshots.exists():
                mapping.last_successful_sync_at = None
                mapping.save(update_fields=["last_successful_sync_at", "updated_at"])
            messages.success(request, f"Выбранные месяцы {label} удалены: {deleted} записей.")
    else:
        return HttpResponseBadRequest("Некорректное действие.")
    return redirect("reports:report-list", project_id=project.id)


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


@login_required
@require_POST
def version_delete(request, version_id):
    version = get_object_or_404(
        ReportVersion.objects.select_related("report__project"), pk=version_id
    )
    report_id = version.report_id
    try:
        number = delete_report_version(version=version)
    except ReportVersionDeleteBlocked as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Версия №{number} удалена.")
    return redirect("reports:report-detail", report_id=report_id)


def _segment_rows(payload, code):
    segments = payload.get("calculated", {}).get("positions", {}).get("segments", [])
    rows = []
    for source in segments:
        row = dict(source)
        row["engine_label"] = ENGINE_LABELS.get(
            source.get("search_engine"), source.get("search_engine") or "Поиск"
        )
        row["topvisor_report_url"] = _topvisor_report_url(payload, source)
        if code in TOP_SECTION_RANGES:
            start, end = TOP_SECTION_RANGES[code]
            configuration_id = source.get("configuration_id")
            candidates = [
                item
                for item in payload.get("ranking_sources", [])
                if item.get("search_engine") == source.get("search_engine")
                and item.get("region") == source.get("region")
                and (not configuration_id or item.get("configuration_id") == configuration_id)
            ]
            latest = max(
                candidates,
                key=lambda item: (item.get("date") or "", item.get("id") or ""),
                default={},
            )
            if code == "top_11_20":
                row["rows"] = source.get("top_11_20", [])
            else:
                depth = latest.get("ranking_depth") or 0
                row["rows"] = [
                    {
                        **item,
                        "position_tone": (
                            "position-top-3"
                            if item.get("position") and item["position"] <= 3
                            else "position-top-5"
                            if item.get("position") and item["position"] <= 5
                            else "position-top-10"
                            if item.get("position") and item["position"] <= 10
                            else "position-top-20"
                            if item.get("position") and item["position"] <= 20
                            else "position-top-30"
                        ),
                    }
                    for item in latest.get("positions", [])
                    if item.get("position") is not None
                    and item["position"] <= depth
                    and start <= item["position"] <= end
                ]
                row["rows"].sort(
                    key=lambda item: (item.get("position"), str(item.get("query", "")).casefold())
                )
                cluster_order = {}
                for item in row["rows"]:
                    cluster = str(item.get("group") or "Без группы")
                    cluster_order.setdefault(cluster, len(cluster_order))
                    item["cluster_tone"] = (
                        "cluster-shade" if cluster_order[cluster] % 2 == 0 else "cluster-white"
                    )
            row_count = len(row["rows"])
            row["row_count"] = row_count
            row["row_word"] = (
                "запрос"
                if row_count % 10 == 1 and row_count % 100 != 11
                else "запроса"
                if row_count % 10 in {2, 3, 4} and row_count % 100 not in {12, 13, 14}
                else "запросов"
            )
        rows.append(row)
    engine_order = {"yandex": 0, "google": 1}
    return sorted(
        rows,
        key=lambda item: (
            engine_order.get(item.get("search_engine"), 99),
            item.get("region") or "",
        ),
    )


def _metric_rows(payload, code):
    if code not in METRIC_SECTIONS:
        return []
    source, codes = METRIC_SECTIONS[code]
    facts = payload.get("calculated", {}).get("sources", {}).get("sources", {}).get(source, {})
    changes = facts.get("normalized_changes", {})
    series = facts.get("three_month_series", {})
    if code == "iks":
        codes = tuple(
            metric_code
            for metric_code in codes
            if any(
                (changes.get(metric_code) or {}).get(field) is not None
                for field in ("current", "previous")
            )
            or any(point.get("value") is not None for point in series.get(metric_code, []))
        )[:1]
    if code == "traffic":
        codes = (*codes, *(key for key in sorted(series) if key.startswith("goal_")))
    if code == "geography":
        options = payload.get("display_options", {})
        selected = {
            "geography_moscow_visits": options.get("geography_moscow", True),
            "geography_saint_petersburg_visits": options.get("geography_saint_petersburg", True),
            "geography_undefined_visits": options.get("geography_undefined", True),
            "geography_area_undefined_visits": options.get("geography_area_undefined", True),
        }
        codes = tuple(metric_code for metric_code in codes if selected[metric_code])
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
        if not section_enabled(payload, code):
            continue
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
                "top_range": TOP_SECTION_RANGES.get(code),
                "modern_report": str(
                    payload.get("display_options", {}).get("configuration_version", 0)
                )
                in {"2", "3"},
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
