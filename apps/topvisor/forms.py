from django import forms

from .services import configuration_id, configuration_segment


def configuration_label(item):
    searcher = item.get("searcher_name", item.get("search_engine", item.get("searcher", "Поиск")))
    region = item.get("region_name", item.get("region", "без региона"))
    depth = item.get("normalized_depth", item.get("depth", item.get("check_depth", 100)))
    return f"{searcher} — {region} (TOP-{depth})"


class TopvisorCredentialsForm(forms.Form):
    user_id = forms.CharField(label="ID пользователя Topvisor", max_length=255)
    api_key = forms.CharField(
        label="API-ключ Topvisor",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )

    def __init__(self, *args, has_key=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_key = has_key

    def clean_api_key(self):
        value = self.cleaned_data["api_key"]
        if not value and not self.has_key:
            raise forms.ValidationError("Введите API-ключ Topvisor.")
        return value


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
                configuration_id(item),
                configuration_label(item),
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
    """Confirmation-only form: report dates never belong to source settings."""
