from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render

from .forms import WorkCategoryForm, WorkLogItemForm
from .models import WorkCategory, WorkLogItem


@staff_member_required
def worklog_list(request):
    items = WorkLogItem.objects.select_related("project", "category", "created_by")[:200]
    return render(request, "worklog/list.html", {"items": items})


@staff_member_required
def worklog_create(request):
    form = WorkLogItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.created_by = request.user
        item.save()
        messages.success(request, "Работа добавлена.")
        return redirect("worklog:list")
    return render(request, "worklog/form.html", {"form": form, "title": "Добавить работу"})


@staff_member_required
def category_create(request):
    form = WorkCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Категория добавлена.")
        return redirect("worklog:list")
    categories = WorkCategory.objects.select_related("project")[:200]
    return render(
        request,
        "worklog/category_form.html",
        {"form": form, "categories": categories},
    )
