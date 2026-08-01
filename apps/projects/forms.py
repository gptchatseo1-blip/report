from django import forms

from .models import Project, ProjectBrandRule, ProjectUrlGroup, ProjectUrlRule


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "domain", "timezone", "language", "active"]


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
