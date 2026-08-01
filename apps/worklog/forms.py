from django import forms

from apps.projects.models import Project

from .models import WorkCategory, WorkLogItem


class WorkCategoryForm(forms.ModelForm):
    class Meta:
        model = WorkCategory
        fields = ["project", "name", "sort_order", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(active=True)


class WorkLogItemForm(forms.ModelForm):
    class Meta:
        model = WorkLogItem
        fields = [
            "project",
            "work_date",
            "category",
            "title",
            "status",
            "url",
            "page_or_material_name",
            "character_count",
            "responsible",
            "comment",
            "result_url",
        ]
        widgets = {"work_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.filter(active=True)
        self.fields["category"].queryset = WorkCategory.objects.filter(active=True).select_related(
            "project"
        )
