from django.contrib import admin

from .forms import ProjectBrandRuleForm, ProjectForm, ProjectUrlGroupForm, ProjectUrlRuleForm
from .models import Project, ProjectBrandRule, ProjectUrlGroup, ProjectUrlRule


class BrandRuleInline(admin.TabularInline):
    model = ProjectBrandRule
    form = ProjectBrandRuleForm
    extra = 1


class UrlGroupInline(admin.TabularInline):
    model = ProjectUrlGroup
    form = ProjectUrlGroupForm
    extra = 1


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    form = ProjectForm
    list_display = ["name", "normalized_domain", "position_provider", "active", "updated_at"]
    list_filter = ["position_provider", "active"]
    search_fields = ["name", "domain", "normalized_domain"]
    readonly_fields = ["normalized_domain", "created_at", "updated_at"]
    inlines = [BrandRuleInline, UrlGroupInline]


class UrlRuleInline(admin.TabularInline):
    model = ProjectUrlRule
    form = ProjectUrlRuleForm
    extra = 1


@admin.register(ProjectUrlGroup)
class ProjectUrlGroupAdmin(admin.ModelAdmin):
    form = ProjectUrlGroupForm
    list_display = ["name", "project", "priority", "active"]
    inlines = [UrlRuleInline]


@admin.register(ProjectBrandRule)
class ProjectBrandRuleAdmin(admin.ModelAdmin):
    form = ProjectBrandRuleForm
    list_display = ["pattern", "project", "kind", "priority", "active"]


@admin.register(ProjectUrlRule)
class ProjectUrlRuleAdmin(admin.ModelAdmin):
    form = ProjectUrlRuleForm
    list_display = ["pattern", "group", "type", "priority", "active"]
