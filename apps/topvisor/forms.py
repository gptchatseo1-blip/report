from datetime import date

from django import forms

from .services import configuration_id, configuration_segment


class MonthInput(forms.DateInput):
    input_type = "month"


class TopvisorProjectForm(forms.Form):
    topvisor_project = forms.ChoiceField(label="Проект Topvisor")
    configurations = forms.MultipleChoiceField(
        label="Поисковые системы и регионы",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, projects=(), configurations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.configurations = {configuration_id(item): item for item in configurations}
        self.fields["topvisor_project"].choices = [
            (str(item["id"]), item.get("name") or item.get("site") or str(item["id"]))
            for item in projects
        ]
        self.fields["configurations"].choices = [
            (
                str(item.get("id") or item.get("searcher_id")),
                f"{item.get('search_engine', item.get('searcher', 'Поиск'))} — "
                f"{item.get('region_name', item.get('region', 'без региона'))} "
                f"(TOP-{item.get('depth', item.get('check_depth', 100))})",
            )
            for item in configurations
        ]

    def clean_configurations(self):
        selected = self.cleaned_data["configurations"]
        seen = set()
        for config_id in selected:
            segment = configuration_segment(self.configurations[config_id])
            if segment in seen:
                raise forms.ValidationError(
                    "Нельзя выбрать несколько конфигураций для одной поисковой системы "
                    "и региона: устройство не является измерением отчёта."
                )
            seen.add(segment)
        return selected


class TopvisorSyncForm(forms.Form):
    month = forms.DateField(label="Отчётный месяц", input_formats=["%Y-%m"], widget=MonthInput())

    def clean_month(self):
        value = self.cleaned_data["month"]
        return date(value.year, value.month, 1)
