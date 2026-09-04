from django import forms

from .models import Project, ProjectBrandRule, ProjectUrlGroup, ProjectUrlRule, normalize_domain


class ProjectDomainFormMixin:
    def clean_domain(self):
        domain = self.cleaned_data["domain"]
        normalized = normalize_domain(domain)
        existing = Project.objects.filter(normalized_domain=normalized)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("Проект с таким доменом уже существует.")
        return domain


class ProjectForm(ProjectDomainFormMixin, forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "name",
            "domain",
            "position_provider",
            "timezone",
            "language",
            "active",
            "top_11_20_mode",
        ]
        widgets = {"position_provider": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position_provider"].required = False
        self.fields["top_11_20_mode"].required = False

    def clean_position_provider(self):
        return self.cleaned_data.get("position_provider") or (
            self.instance.position_provider
            if self.instance.pk
            else Project.PositionProvider.TOPVISOR
        )

    def clean_top_11_20_mode(self):
        return self.cleaned_data.get("top_11_20_mode") or (
            self.instance.top_11_20_mode if self.instance.pk else Project.Top1120Mode.AUTO
        )


class ProjectQuickCreateForm(ProjectDomainFormMixin, forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "domain", "position_provider"]
        widgets = {"position_provider": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["position_provider"].required = False

    def clean_position_provider(self):
        return self.cleaned_data.get("position_provider") or Project.PositionProvider.TOPVISOR


class ProjectBrandRuleForm(forms.ModelForm):
    class Meta:
        model = ProjectBrandRule
        fields = ["project", "kind", "pattern", "priority", "active"]


class ProjectUrlGroupForm(forms.ModelForm):
    class Meta:
        model = ProjectUrlGroup
        fields = ["project", "name", "slug", "priority", "active"]


class ProjectUrlRuleForm(forms.ModelForm):
    class Meta:
        model = ProjectUrlRule
        fields = ["group", "type", "pattern", "priority", "active"]
