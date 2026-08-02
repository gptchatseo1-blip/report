from datetime import date

from django import forms

from .models import NarrativeBlock


class MonthInput(forms.DateInput):
    input_type = "month"


class ReportCreateForm(forms.Form):
    month = forms.DateField(label="Отчётный месяц", input_formats=["%Y-%m"], widget=MonthInput())

    def clean_month(self):
        value = self.cleaned_data["month"]
        return date(value.year, value.month, 1)


class NarrativeEditForm(forms.ModelForm):
    edited_text = forms.CharField(
        label="Редакция",
        required=False,
        max_length=10_000,
        widget=forms.Textarea(attrs={"rows": 6}),
    )

    class Meta:
        model = NarrativeBlock
        fields = ("edited_text", "status")
        labels = {"status": "Статус"}
