from django import forms

from apps.projects.models import Project


class SyntheticSyncForm(forms.Form):
    project = forms.ModelChoiceField(label="Проект", queryset=Project.objects.none())
    report_month = forms.DateField(
        label="Отчётный месяц",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(active=True)

    def clean_report_month(self):
        return self.cleaned_data["report_month"].replace(day=1)
