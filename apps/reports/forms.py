from collections import defaultdict
from datetime import date

from django import forms
from django.utils import timezone

from apps.topvisor.models import TopvisorProjectMapping
from apps.topvisor.services import configuration_id

from .models import NarrativeBlock


class ReportCreateForm(forms.Form):
    submission_token = forms.CharField(widget=forms.HiddenInput(), required=False)
    month = forms.DateField(required=False, input_formats=["%Y-%m"], widget=forms.HiddenInput())
    yandex_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    google_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    show_urls = forms.BooleanField(label="Выводить URL", required=False, initial=False)
    include_visibility = forms.BooleanField(label="Видимость сайта", required=False, initial=True)
    include_monthly_dynamics = forms.BooleanField(
        label="Динамика по месяцам", required=False, initial=True
    )
    include_top_tables = forms.BooleanField(
        label="Таблицы запросов по позициям", required=False, initial=True
    )
    include_top_5 = forms.BooleanField(label="TOP-5", required=False, initial=True)
    include_top_10 = forms.BooleanField(label="TOP-10", required=False, initial=True)
    include_top_20 = forms.BooleanField(label="TOP-20", required=False, initial=False)
    include_top_11_30 = forms.BooleanField(label="TOP-11–30", required=False, initial=True)
    include_top_30 = forms.BooleanField(label="TOP-30", required=False, initial=False)
    include_topvisor_report_link = forms.BooleanField(
        label="Ссылка на подробный отчёт Topvisor", required=False, initial=False
    )
    topvisor_report_url = forms.URLField(
        label="Ссылка на отчёт Topvisor",
        required=False,
        assume_scheme="https",
        widget=forms.URLInput(attrs={"placeholder": "https://..."}),
    )
    include_webmaster = forms.BooleanField(
        label="Данные Яндекс.Вебмастера", required=False, initial=True
    )
    webmaster_chart_period = forms.ChoiceField(
        label="Период графиков Вебмастера",
        required=False,
        initial="report_month",
        choices=(
            ("report_month", "Только отчётный месяц"),
            ("selected", "Весь выбранный диапазон"),
        ),
    )
    include_webmaster_popular_queries = forms.BooleanField(
        label="Самые кликабельные запросы", required=False, initial=True
    )
    webmaster_queries_screenshot = forms.ImageField(
        label="Скриншот таблицы запросов",
        required=False,
        help_text="Используется, если API не вернул список популярных запросов.",
    )
    webmaster_queries_comment = forms.CharField(
        label="Комментарий к самым кликабельным запросам",
        required=False,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    include_metrika = forms.BooleanField(
        label="Данные Яндекс.Метрики", required=False, initial=True
    )
    metrika_robotness = forms.ChoiceField(
        label="Роботность",
        required=False,
        initial="humans",
        choices=(("humans", "Только люди"), ("all", "Люди и роботы")),
    )
    include_metrika_sources_table = forms.BooleanField(
        label="Таблица по всем источникам", required=False, initial=False
    )
    include_metrika_search_engines = forms.BooleanField(
        label="Поисковые системы", required=False, initial=True
    )
    include_metrika_geography = forms.BooleanField(
        label="География посетителей", required=False, initial=True
    )
    geography_moscow = forms.BooleanField(label="Москва", required=False, initial=True)
    geography_saint_petersburg = forms.BooleanField(
        label="Санкт-Петербург", required=False, initial=True
    )
    geography_undefined = forms.BooleanField(label="Не определено", required=False, initial=True)
    geography_area_undefined = forms.BooleanField(
        label="Область не определена", required=False, initial=True
    )
    include_metrika_landing_pages = forms.BooleanField(
        label="Популярные страницы входа", required=False, initial=True
    )
    include_metrika_landing_page_comparison = forms.BooleanField(
        label="Страницы входа по Яндексу и Google в сравнении", required=False, initial=False
    )
    include_metrika_url_groups = forms.BooleanField(
        label="Информационные и коммерческие разделы", required=False, initial=False
    )
    include_metrika_sections = forms.BooleanField(
        label="Данные по разделам", required=False, initial=False
    )
    include_metrika_categories = forms.BooleanField(
        label="Основные прорабатываемые категории", required=False, initial=False
    )
    include_metrika_goals = forms.BooleanField(label="Цели Метрики", required=False, initial=True)
    include_completed_work = forms.BooleanField(
        label="Выполненные работы", required=False, initial=True
    )
    metrika_snapshots = forms.MultipleChoiceField(
        label="Яндекс.Метрика", required=False, widget=forms.CheckboxSelectMultiple
    )
    webmaster_snapshots = forms.MultipleChoiceField(
        label="Яндекс.Вебмастер", required=False, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.connected_engines = set()
        self.connected_sources = set()
        self.source_availability = {}
        self.source_period_options = {}
        if project is None:
            for name in (
                "yandex_dates",
                "google_dates",
                "metrika_snapshots",
                "webmaster_snapshots",
            ):
                self.fields[name].choices = []
            return
        from apps.metrics.models import RankingSnapshot, SourceSnapshot

        for source, relation in (
            (SourceSnapshot.Source.METRIKA, "yandex_metrika_mapping"),
            (SourceSnapshot.Source.WEBMASTER, "yandex_webmaster_mapping"),
        ):
            if hasattr(project, relation):
                self.connected_sources.add(source)

        required = defaultdict(set)
        try:
            configurations = project.topvisor_mapping.selected_configurations
        except TopvisorProjectMapping.DoesNotExist:
            configurations = []
        for item in configurations:
            raw = str(
                item.get("search_engine")
                or item.get("searcher_name")
                or item.get("searcher")
                or item.get("id", "")
            ).casefold()
            engine = (
                "yandex"
                if "yandex" in raw or "яндекс" in raw
                else "google"
                if "google" in raw
                else ""
            )
            if engine:
                required[engine].add(configuration_id(item))
        self.connected_engines = set(required)
        for engine in ("yandex", "google"):
            available = []
            if required[engine]:
                rows = RankingSnapshot.objects.filter(
                    project=project,
                    search_engine__iexact=engine,
                    topvisor_configuration_id__in=required[engine],
                ).values_list("snapshot_date", "topvisor_configuration_id")
                by_date = defaultdict(set)
                for day, config in rows:
                    by_date[day].add(config)
                available = sorted(
                    (day for day, configs in by_date.items() if configs == required[engine]),
                    reverse=True,
                )
            self.fields[f"{engine}_dates"].choices = [
                (d.isoformat(), d.strftime("%d.%m.%Y")) for d in available
            ]
        latest_ranking = (
            RankingSnapshot.objects.filter(project=project).order_by("-snapshot_date").first()
        )
        report_month = (
            latest_ranking.snapshot_date.replace(day=1)
            if latest_ranking
            else timezone.localdate().replace(day=1)
        )
        month_indexes = {
            report_month.year * 12 + report_month.month - 1 - offset for offset in range(3)
        }
        for field, source in (
            ("metrika_snapshots", SourceSnapshot.Source.METRIKA),
            ("webmaster_snapshots", SourceSnapshot.Source.WEBMASTER),
        ):
            rows = list(
                SourceSnapshot.objects.filter(project=project, source=source).order_by(
                    "-period_start", "-period_end", "id"
                )
            )
            self.fields[field].choices = [
                (str(row.id), f"{row.period_start:%d.%m.%Y} — {row.period_end:%d.%m.%Y}")
                for row in rows
            ]
            self.source_period_options[field] = [
                {
                    "id": str(row.id),
                    "month": row.period_start.strftime("%Y-%m"),
                    "label": f"{row.period_start:%d.%m.%Y} — {row.period_end:%d.%m.%Y}",
                }
                for row in rows
            ]
            defaults = []
            selected_months = set()
            for row in rows:
                row_month = row.period_start.year * 12 + row.period_start.month - 1
                if row_month in month_indexes and row_month not in selected_months:
                    defaults.append(str(row.id))
                    selected_months.add(row_month)
            self.source_availability[source] = {
                "connected": source in self.connected_sources,
                "count": len(rows),
                "selected_count": len(defaults),
            }
            # Never overwrite submitted checkbox values on a bound form.
            if not self.is_bound:
                self.initial.setdefault(field, defaults)

    def clean_month(self):
        value = self.cleaned_data.get("month")
        return date(value.year, value.month, 1) if value else value

    def clean(self):
        cleaned = super().clean()
        for engine, label in (("yandex", "Яндекс"), ("google", "Google")):
            if engine in self.connected_engines and len(cleaned.get(f"{engine}_dates", [])) < 2:
                self.add_error(f"{engine}_dates", f"{label}: выберите минимум две доступные даты.")
            cleaned[f"{engine}_dates"] = sorted(cleaned.get(f"{engine}_dates", []))
        for field, source, label in (
            ("metrika_snapshots", "yandex_metrika", "Метрика"),
            ("webmaster_snapshots", "yandex_webmaster", "Вебмастер"),
        ):
            selected = cleaned.get(field, [])
            availability = self.source_availability.get(source, {})
            if availability.get("connected") and availability.get("count") and not selected:
                self.add_error(
                    field,
                    f"{label}: выберите хотя бы один синхронизированный период или "
                    "синхронизируйте источник заново.",
                )
            cleaned[field] = sorted(selected)
        if cleaned.get("include_topvisor_report_link") and not cleaned.get("topvisor_report_url"):
            self.add_error(
                "topvisor_report_url",
                "Укажите ссылку или отключите её вывод в отчёте.",
            )
        if cleaned.get("include_top_tables") and not any(
            cleaned.get(name)
            for name in (
                "include_top_5",
                "include_top_10",
                "include_top_20",
                "include_top_11_30",
                "include_top_30",
            )
        ):
            self.add_error("include_top_tables", "Выберите хотя бы один диапазон TOP.")
        return cleaned

    def clean_webmaster_queries_screenshot(self):
        image = self.cleaned_data.get("webmaster_queries_screenshot")
        if image and image.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Размер скриншота не должен превышать 5 МБ.")
        return image


class NarrativeEditForm(forms.ModelForm):
    edited_text = forms.CharField(
        label="Редакция",
        required=False,
        max_length=10_000,
        widget=forms.Textarea(attrs={"rows": 6}),
    )

    class Meta:
        model = NarrativeBlock
        fields = ("edited_text",)
