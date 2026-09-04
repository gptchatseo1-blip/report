from django import forms

from .models import SerphuntProjectMapping


class SerphuntCredentialsForm(forms.Form):
    api_key = forms.CharField(
        label="API-ключ Serphunt", required=False, widget=forms.PasswordInput(render_value=False)
    )

    def __init__(self, *args, has_key=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_key = has_key

    def clean_api_key(self):
        value = self.cleaned_data["api_key"].strip()
        if value.casefold().startswith("bearer "):
            value = value[7:].strip()
        if not value and not self.has_key:
            raise forms.ValidationError("Введите API-ключ Serphunt.")
        return value


class SerphuntProjectForm(forms.ModelForm):
    search_engines = forms.MultipleChoiceField(
        label="Поисковые системы",
        choices=(("yandex", "Яндекс"), ("google", "Google")),
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = SerphuntProjectMapping
        fields = (
            "keywords",
            "search_engines",
            "region_id",
            "region_name",
            "device",
            "language",
            "google_search_depth",
            "include_subdomains",
        )
        widgets = {
            "keywords": forms.Textarea(
                attrs={"rows": 12, "placeholder": "Один ключевой запрос на строку"}
            ),
            "device": forms.Select(choices=(("desktop", "Компьютеры"), ("mobile", "Мобильные"))),
            "google_search_depth": forms.Select(
                choices=tuple((value, f"TOP-{value}") for value in range(10, 101, 10))
            ),
        }

    def clean_keywords(self):
        rows = list(
            dict.fromkeys(
                line.strip() for line in self.cleaned_data["keywords"].splitlines() if line.strip()
            )
        )
        if not rows:
            raise forms.ValidationError("Добавьте хотя бы один ключевой запрос.")
        if len(rows) > 5000:
            raise forms.ValidationError("За один запуск можно отправить не более 5000 запросов.")
        return "\n".join(rows)
