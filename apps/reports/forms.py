from datetime import date

from django import forms
from django.db.models import Count

from apps.topvisor.models import TopvisorProjectMapping

from .models import NarrativeBlock


class MonthInput(forms.DateInput):
    input_type = "month"


class ReportCreateForm(forms.Form):
    submission_token = forms.CharField(widget=forms.HiddenInput(), required=False)
    month = forms.DateField(required=False, input_formats=["%Y-%m"], widget=forms.HiddenInput())
    topvisor_dates = forms.MultipleChoiceField(
        label="Topvisor", required=False, widget=forms.CheckboxSelectMultiple
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
        if project is None:
            for name in ("topvisor_dates", "metrika_snapshots", "webmaster_snapshots"):
                self.fields[name].choices = []
            return
        from apps.metrics.models import RankingSnapshot, SourceSnapshot

        try:
            mapping = project.topvisor_mapping
            configuration_ids = {
                str(item.get("id") or f"{item.get('searcher_id')}:{item.get('region_id', '')}")
                for item in mapping.selected_configurations
            }
        except TopvisorProjectMapping.DoesNotExist:
            configuration_ids = set()
        dates = []
        if configuration_ids:
            candidates = (
                RankingSnapshot.objects.filter(
                    project=project, topvisor_configuration_id__in=configuration_ids
                )
                .values("snapshot_date")
                .annotate(configuration_count=Count("topvisor_configuration_id", distinct=True))
                .filter(configuration_count=len(configuration_ids))
                .order_by("-snapshot_date")
            )
            dates = [item["snapshot_date"] for item in candidates]
        self.fields["topvisor_dates"].choices = [
            (d.isoformat(), d.strftime("%d.%m.%Y")) for d in dates
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

    def clean_topvisor_dates(self):
        values = sorted(self.cleaned_data["topvisor_dates"])
        if self.fields["topvisor_dates"].choices and len(values) < 2:
            raise forms.ValidationError("Выберите не менее двух фактических дат Topvisor.")
        return values


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
