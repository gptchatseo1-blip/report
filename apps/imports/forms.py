from pathlib import Path

from django import forms
from django.conf import settings

from apps.projects.models import Project

from .models import ImportBatch


class PositionImportForm(forms.Form):
    project = forms.ModelChoiceField(
        label="Проект", queryset=Project.objects.none(), empty_label="Выберите проект"
    )
    snapshot_date = forms.DateField(
        label="Дата позиций", widget=forms.DateInput(attrs={"type": "date"})
    )
    search_engine = forms.ChoiceField(
        label="Поисковая система", choices=ImportBatch.SearchEngine.choices
    )
    region = forms.CharField(label="Регион", max_length=120)
    source_file = forms.FileField(label="CSV или XLSX")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(active=True).order_by("name")

    def clean_source_file(self):
        uploaded_file = self.cleaned_data["source_file"]
        extension = Path(uploaded_file.name).suffix.casefold()
        if extension not in {".csv", ".xlsx"}:
            raise forms.ValidationError("Поддерживаются только файлы CSV и XLSX.")
        if uploaded_file.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise forms.ValidationError(f"Файл больше {settings.MAX_UPLOAD_SIZE_MB} МБ.")
        return uploaded_file
