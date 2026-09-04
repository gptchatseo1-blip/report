import json
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlsplit

from django import forms
from django.utils import timezone

from apps.topvisor.models import TopvisorProjectMapping
from apps.topvisor.services import configuration_id

from .models import NarrativeBlock
from .rich_text import sanitize_report_html

PERSISTED_REPORT_FIELDS = (
    "show_urls",
    "include_visibility",
    "include_visibility_table",
    "include_monthly_dynamics",
    "include_monthly_dynamics_table",
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
    "webmaster_queries_comment",
    "include_metrika",
    "metrika_robotness",
    "metrika_search_segment",
    "metrika_search_attribution",
    "include_metrika_sources_table",
    "metrika_sources_compare_previous",
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
    "topvisor_manual_rows",
    "metrika_info_url_groups",
    "metrika_commercial_url_groups",
    "metrika_category_url_groups",
    "metrika_subsection_url_groups",
    "metrika_landing_comparison_subsection_url_groups",
    "include_completed_work",
    "completed_work_text",
    "sync_log_retention_months",
)

BOOLEAN_REPORT_FIELDS = frozenset(
    name
    for name in PERSISTED_REPORT_FIELDS
    if name.startswith("include_")
    or name
    in {
        "show_urls",
        "metrika_search_segment",
        "metrika_sources_compare_previous",
        "geography_moscow",
        "geography_moscow_region",
        "geography_saint_petersburg",
        "geography_saint_petersburg_region",
        "geography_undefined",
        "geography_area_undefined",
        "metrika_categories_combined",
        "metrika_goals_quarter",
    }
)


def parse_named_url_groups(value):
    """Parse user-friendly `name | mask` lines into deterministic URL groups."""
    groups = {}
    for line_number, raw_line in enumerate(str(value or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            label, pattern = (part.strip() for part in line.split("|", 1))
        else:
            pattern = line
            parsed = urlsplit(pattern.replace("*", ""))
            parts = [part for part in parsed.path.split("/") if part]
            label = parts[-1].replace("-", " ").strip().capitalize() if parts else parsed.netloc
        if not label or not pattern:
            raise forms.ValidationError(
                f"Строка {line_number}: укажите название и URL через символ |."
            )
        if len(label) > 200 or len(pattern) > 2000:
            raise forms.ValidationError(f"Строка {line_number}: значение слишком длинное.")
        groups.setdefault(label, [])
        if pattern not in groups[label]:
            groups[label].append(pattern)
    return [{"name": name, "patterns": patterns} for name, patterns in groups.items()]


def validate_topvisor_manual_rows(value):
    try:
        rows = json.loads(value or "[]") if isinstance(value, str) else value
    except json.JSONDecodeError:
        raise forms.ValidationError("Некорректные ручные значения Topvisor.") from None
    if not isinstance(rows, list) or len(rows) > 500:
        raise forms.ValidationError("Некорректные ручные значения Topvisor.")

    def number(raw, *, maximum, integer=False, label="значение"):
        try:
            result = float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            raise forms.ValidationError(f"Некорректное поле «{label}» в ручной строке.") from None
        if result < 0 or result > maximum or (integer and not result.is_integer()):
            raise forms.ValidationError(f"Некорректное поле «{label}» в ручной строке.")
        return int(round(result)) if integer else result

    cleaned = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise forms.ValidationError("Некорректная ручная строка динамики.")
        engine = str(row.get("engine") or "").casefold()[:16]
        region = str(row.get("region") or "").strip()[:120]
        month = str(row.get("month") or "")[:7]
        try:
            month = date.fromisoformat(f"{month}-01").isoformat() if month else ""
        except ValueError:
            raise forms.ValidationError("Укажите корректный месяц в ручной строке.") from None
        if not month:
            raise forms.ValidationError("Месяц в ручной строке обязателен.")
        if not engine:
            raise forms.ValidationError("Поисковая система в ручной строке обязательна.")
        key = (engine, region.casefold(), month[:7])
        if key in seen:
            raise forms.ValidationError(
                "Для одной поисковой системы и региона месяц не должен повторяться."
            )
        seen.add(key)
        cleaned.append(
            {
                "configuration_id": str(row.get("configuration_id") or "")[:120],
                "engine": engine,
                "region": region,
                "month": month,
                "visibility": number(row.get("visibility", 0), maximum=100, label="видимость"),
                "total": number(
                    row.get("total", 0), maximum=10_000_000, integer=True, label="всего"
                ),
                "top3": number(
                    row.get("top3", 0), maximum=10_000_000, integer=True, label="в топ 3"
                ),
                "top10": number(
                    row.get("top10", 0), maximum=10_000_000, integer=True, label="в топ 10"
                ),
                "top11_30": number(
                    row.get("top11_30", 0), maximum=10_000_000, integer=True, label="в топ 11–30"
                ),
                "top3_percent": number(
                    row.get("top3_percent", 0), maximum=100, label="процент в топ 3"
                ),
                "top10_percent": number(
                    row.get("top10_percent", 0), maximum=100, label="процент в топ 10"
                ),
                "top11_30_percent": number(
                    row.get("top11_30_percent", 0), maximum=100, label="процент в топ 11–30"
                ),
            }
        )
    return sorted(cleaned, key=lambda row: (row["engine"], row["region"].casefold(), row["month"]))


class ReportCreateForm(forms.Form):
    submission_token = forms.CharField(widget=forms.HiddenInput(), required=False)
    month = forms.DateField(
        label="Отчётный месяц",
        required=False,
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    yandex_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    google_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    show_urls = forms.BooleanField(label="Выводить URL", required=False, initial=False)
    topvisor_manual_rows = forms.CharField(required=False, widget=forms.HiddenInput())
    include_visibility = forms.BooleanField(label="Видимость сайта", required=False, initial=True)
    include_visibility_table = forms.BooleanField(
        label="Таблица позиций по выбранным дням", required=False, initial=False
    )
    include_monthly_dynamics = forms.BooleanField(
        label="Динамика по месяцам", required=False, initial=True
    )
    include_monthly_dynamics_table = forms.BooleanField(
        label="Выводить таблицу динамики", required=False, initial=True
    )
    include_top_tables = forms.BooleanField(
        label="Таблицы запросов по позициям", required=False, initial=True
    )
    include_top_5 = forms.BooleanField(label="TOP-5", required=False, initial=False)
    include_top_10 = forms.BooleanField(label="TOP-10", required=False, initial=True)
    include_top_20 = forms.BooleanField(label="TOP-20", required=False, initial=False)
    include_top_11_30 = forms.BooleanField(label="TOP-11–30", required=False, initial=False)
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
    metrika_search_segment = forms.BooleanField(label="Сегмент ПС", required=False, initial=True)
    metrika_search_attribution = forms.ChoiceField(
        label="Модель атрибуции",
        required=False,
        initial="lastsign",
        choices=(
            ("automatic", "Автоматическая"),
            ("last", "Последний переход"),
            ("lastsign", "Последний значимый переход"),
        ),
    )
    include_metrika_sources_table = forms.BooleanField(
        label="Таблица по всем источникам", required=False, initial=False
    )
    metrika_sources_compare_previous = forms.BooleanField(
        label="Сравнение с предыдущим месяцем", required=False, initial=False
    )
    include_metrika_search_engines = forms.BooleanField(
        label="Поисковые системы", required=False, initial=True
    )
    metrika_bar_search_engines = forms.MultipleChoiceField(
        label="Поисковые системы в столбчатом графике",
        required=False,
        initial=("google", "yandex"),
        choices=(
            ("google", "Google"),
            ("yandex", "Яндекс"),
            ("bing", "Bing"),
            ("yahoo", "Yahoo"),
        ),
        widget=forms.CheckboxSelectMultiple,
    )
    include_metrika_geography = forms.BooleanField(
        label="География посетителей", required=False, initial=True
    )
    geography_moscow = forms.BooleanField(label="Москва", required=False, initial=True)
    geography_moscow_region = forms.BooleanField(
        label="Москва и Московская область", required=False, initial=False
    )
    geography_saint_petersburg = forms.BooleanField(
        label="Санкт-Петербург", required=False, initial=True
    )
    geography_saint_petersburg_region = forms.BooleanField(
        label="Санкт-Петербург и Ленинградская область", required=False, initial=False
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
    metrika_categories_combined = forms.BooleanField(
        label="Выводить на одном графике", required=False, initial=False
    )
    metrika_info_url_groups = forms.CharField(
        label="Информационные разделы",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Статьи | https://site.ru/articles/*\nFAQ | https://site.ru/faq/*",
            }
        ),
        help_text=(
            "Одна строка: название раздела | URL или маска. Одинаковое название можно повторять."
        ),
    )
    metrika_commercial_url_groups = forms.CharField(
        label="Коммерческие разделы",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": "Лечение | https://site.ru/treatment/*\nДиагностика | https://site.ru/diagnostics/*",
            }
        ),
        help_text="Одна строка: название раздела | URL или маска.",
    )
    metrika_category_url_groups = forms.CharField(
        label="Прорабатываемые категории",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "Оперативная гинекология | https://site.ru/medicine/operativnaya-ginekologiya/*",
            }
        ),
        help_text="Для каждой категории задайте название и её URL/маски.",
    )
    metrika_subsection_url_groups = forms.CharField(
        label="Популярные подразделы",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "УЗИ | https://site.ru/diagnostika/uzi/*\nМРТ | https://site.ru/diagnostika/mrt/*",
            }
        ),
        help_text="Название и URL подраздела. Эти URL раскрываются вторым уровнем в таблицах.",
    )
    metrika_landing_comparison_subsection_url_groups = forms.CharField(
        label="Подразделы для страниц входа: Яндекс и Google",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "УЗИ | https://site.ru/diagnostika/uzi/*\nМРТ | https://site.ru/diagnostika/mrt/*",
            }
        ),
        help_text=(
            "URL-группы, используемые для детализации таблиц и текстовых выводов блока "
            "«Страницы входа: Яндекс и Google»."
        ),
    )
    include_metrika_goals = forms.BooleanField(label="Цели Метрики", required=False, initial=True)
    metrika_goals_quarter = forms.BooleanField(
        label="Выводить значения за квартал", required=False, initial=True
    )
    include_completed_work = forms.BooleanField(
        label="Выполненные работы", required=False, initial=True
    )
    completed_work_text = forms.CharField(
        label="Текст выполненных работ",
        required=False,
        max_length=20_000,
        widget=forms.Textarea(attrs={"rows": 8, "class": "rich-text-source"}),
    )
    sync_log_retention_months = forms.ChoiceField(
        label="Автоматически очищать журнал синхронизации",
        required=False,
        initial="12",
        choices=(
            ("6", "Старше 6 месяцев"),
            ("12", "Старше 12 месяцев"),
            ("forever", "Не удалять автоматически"),
        ),
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
        self.topvisor_report_link_fields = []
        if project is None:
            for name in (
                "yandex_dates",
                "google_dates",
                "metrika_snapshots",
                "webmaster_snapshots",
            ):
                self.fields[name].choices = []
            return
        saved = getattr(getattr(project, "report_settings", None), "values", {}) or {}
        if not self.is_bound:
            for name in PERSISTED_REPORT_FIELDS:
                if name in saved and name in self.fields:
                    self.initial[name] = saved[name]

        from apps.metrics.models import RankingSnapshot, SourceSnapshot

        for source, relation in (
            (SourceSnapshot.Source.METRIKA, "yandex_metrika_mapping"),
            (SourceSnapshot.Source.WEBMASTER, "yandex_webmaster_mapping"),
        ):
            if hasattr(project, relation):
                self.connected_sources.add(source)

        required = defaultdict(set)
        if project.position_provider == project.PositionProvider.SERPHUNT:
            try:
                from apps.serphunt.services import configurations as serphunt_configurations

                configurations = serphunt_configurations(project.serphunt_mapping)
            except (ImportError, AttributeError):
                configurations = []
        else:
            try:
                configurations = project.topvisor_mapping.selected_configurations
            except TopvisorProjectMapping.DoesNotExist:
                configurations = []
        for index, configuration in enumerate(
            configurations if project.position_provider == project.PositionProvider.TOPVISOR else []
        ):
            raw_engine = str(
                configuration.get("search_engine")
                or configuration.get("searcher_name")
                or configuration.get("searcher")
                or ""
            )
            normalized_engine = raw_engine.casefold()
            engine_label = (
                "Яндекс"
                if "yandex" in normalized_engine or "яндекс" in normalized_engine
                else "Google"
                if "google" in normalized_engine
                else raw_engine or "Поисковая система"
            )
            region = str(
                configuration.get("region_name") or configuration.get("region") or ""
            ).strip()
            field_name = f"topvisor_report_url_{index}"
            self.fields[field_name] = forms.URLField(
                label=f"{engine_label} · {region or 'Регион не указан'}",
                required=False,
                assume_scheme="https",
                widget=forms.URLInput(
                    attrs={
                        "placeholder": "https://...",
                        "data-topvisor-configuration-id": configuration_id(configuration),
                    }
                ),
            )
            self.topvisor_report_link_fields.append(
                {
                    "name": field_name,
                    "configuration_id": configuration_id(configuration),
                    "engine_label": engine_label,
                    "region": region,
                }
            )
            if not self.is_bound:
                saved_urls = saved.get("topvisor_report_urls") or {}
                saved_url = saved_urls.get(str(configuration_id(configuration)))
                if saved_url:
                    self.initial[field_name] = saved_url
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
        current_month = timezone.localdate().replace(day=1)
        previous_month = (current_month - timedelta(days=1)).replace(day=1)
        latest_month = latest_ranking.snapshot_date.replace(day=1) if latest_ranking else None
        report_month = min(latest_month, previous_month) if latest_month else previous_month
        self.report_month = report_month
        if not self.is_bound:
            self.initial.setdefault("month", report_month)
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

    def clean_topvisor_manual_rows(self):
        return json.dumps(
            validate_topvisor_manual_rows(self.cleaned_data.get("topvisor_manual_rows") or "[]"),
            ensure_ascii=False,
        )

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
            include_field = "include_metrika" if source == "yandex_metrika" else "include_webmaster"
            if (
                cleaned.get(include_field)
                and availability.get("connected")
                and availability.get("count")
                and not selected
            ):
                self.add_error(
                    field,
                    f"{label}: выберите хотя бы один синхронизированный период или "
                    "синхронизируйте источник заново.",
                )
            cleaned[field] = sorted(selected)
        if cleaned.get("include_topvisor_report_link"):
            legacy_url = cleaned.get("topvisor_report_url")
            if self.topvisor_report_link_fields:
                for item in self.topvisor_report_link_fields:
                    if not cleaned.get(item["name"]) and legacy_url:
                        cleaned[item["name"]] = legacy_url
                    if not cleaned.get(item["name"]):
                        self.add_error(
                            item["name"],
                            "Укажите ссылку для этой поисковой системы и региона.",
                        )
            elif not legacy_url:
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
        if cleaned.get("include_metrika_search_engines") and not cleaned.get(
            "metrika_bar_search_engines"
        ):
            self.add_error(
                "metrika_bar_search_engines",
                "Выберите минимум одну поисковую систему для столбчатого графика.",
            )
        return cleaned

    def cleaned_topvisor_report_urls(self):
        return {
            str(item["configuration_id"]): self.cleaned_data.get(item["name"])
            for item in self.topvisor_report_link_fields
            if self.cleaned_data.get(item["name"])
        }

    def clean_webmaster_queries_screenshot(self):
        image = self.cleaned_data.get("webmaster_queries_screenshot")
        if image and image.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Размер скриншота не должен превышать 5 МБ.")
        return image

    def clean_metrika_info_url_groups(self):
        value = self.cleaned_data.get("metrika_info_url_groups", "")
        parse_named_url_groups(value)
        return value

    def clean_metrika_commercial_url_groups(self):
        value = self.cleaned_data.get("metrika_commercial_url_groups", "")
        parse_named_url_groups(value)
        return value

    def clean_metrika_category_url_groups(self):
        value = self.cleaned_data.get("metrika_category_url_groups", "")
        parse_named_url_groups(value)
        return value

    def clean_metrika_subsection_url_groups(self):
        value = self.cleaned_data.get("metrika_subsection_url_groups", "")
        parse_named_url_groups(value)
        return value

    def clean_metrika_landing_comparison_subsection_url_groups(self):
        value = self.cleaned_data.get("metrika_landing_comparison_subsection_url_groups", "")
        parse_named_url_groups(value)
        return value

    def clean_completed_work_text(self):
        return sanitize_report_html(self.cleaned_data.get("completed_work_text"))


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
