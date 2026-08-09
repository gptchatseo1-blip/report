from django import forms


class CounterForm(forms.Form):
    counter_id = forms.CharField(widget=forms.HiddenInput)
    counter_name = forms.CharField(widget=forms.HiddenInput)
    counter_domain = forms.CharField(widget=forms.HiddenInput)
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
