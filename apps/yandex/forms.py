import urllib.parse

from django import forms
from django.urls import reverse


class YandexOAuthCredentialsForm(forms.Form):
    client_id = forms.CharField(label="ClientID", max_length=255)
    client_secret = forms.CharField(
        label="Client secret",
        required=False,
        widget=forms.PasswordInput(render_value=False),
    )
    redirect_uri = forms.URLField(
        label="Redirect URI",
        max_length=2000,
        assume_scheme="https",
        widget=forms.URLInput(attrs={"placeholder": "https://example.ru/yandex/oauth/callback/"}),
    )

    def __init__(self, *args, has_secret=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_secret = has_secret

    def clean_client_secret(self):
        value = self.cleaned_data["client_secret"]
        if not value and not self.has_secret:
            raise forms.ValidationError("Введите Client secret.")
        return value

    def clean_redirect_uri(self):
        value = self.cleaned_data["redirect_uri"]
        parsed = urllib.parse.urlsplit(value)
        if parsed.path != reverse("yandex:oauth-callback") or parsed.query or parsed.fragment:
            raise forms.ValidationError(
                "Redirect URI должен содержать точный путь /yandex/oauth/callback/ "
                "без параметров и фрагмента."
            )
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise forms.ValidationError("Для Redirect URI требуется HTTPS.")
        return value


class CounterForm(forms.Form):
    counter_id = forms.CharField(widget=forms.HiddenInput)
    confirm_domain_mismatch = forms.BooleanField(
        required=False, label="Подтверждаю выбор счётчика другого домена"
    )


class GoalsForm(forms.Form):
    goals = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple, label="Цели"
    )

    def __init__(self, *args, available_goals=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["goals"].choices = [
            (str(g["id"]), g.get("name", str(g["id"]))) for g in available_goals
        ]


class SyncForm(forms.Form):
    month = forms.DateField(
        input_formats=["%Y-%m"], widget=forms.DateInput(attrs={"type": "month"})
    )
    force_refresh = forms.BooleanField(required=False)


class HostForm(forms.Form):
    host_id = forms.CharField(widget=forms.HiddenInput)
    confirm_domain_mismatch = forms.BooleanField(
        required=False, label="Подтверждаю выбор сайта другого домена"
    )
