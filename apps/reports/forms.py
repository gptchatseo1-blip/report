from collections import defaultdict
from datetime import date

from django import forms

from apps.topvisor.models import TopvisorProjectMapping
from apps.topvisor.services import configuration_id

from .models import NarrativeBlock


class ReportCreateForm(forms.Form):
    submission_token = forms.CharField(widget=forms.HiddenInput(), required=False)
    month = forms.DateField(required=False, input_formats=["%Y-%m"], widget=forms.HiddenInput())
    yandex_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    google_dates = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    show_urls = forms.BooleanField(label="Выводить URL", required=False, initial=False)
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
        for field, source in (
            ("metrika_snapshots", SourceSnapshot.Source.METRIKA),
            ("webmaster_snapshots", SourceSnapshot.Source.WEBMASTER),
        ):
            rows = SourceSnapshot.objects.filter(project=project, source=source).order_by(
                "-period_start"
            )
            self.fields[field].choices = [
                (str(row.id), f"{row.period_start:%d.%m.%Y} — {row.period_end:%d.%m.%Y}")
                for row in rows
            ]

    def clean_month(self):
        value = self.cleaned_data.get("month")
        return date(value.year, value.month, 1) if value else value

    def clean(self):
        cleaned = super().clean()
        for engine, label in (("yandex", "Яндекс"), ("google", "Google")):
            if engine in self.connected_engines and len(cleaned.get(f"{engine}_dates", [])) < 2:
                self.add_error(f"{engine}_dates", f"{label}: выберите минимум две доступные даты.")
            cleaned[f"{engine}_dates"] = sorted(cleaned.get(f"{engine}_dates", []))
        return cleaned


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
