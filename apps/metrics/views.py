from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render

from .forms import SyntheticSyncForm
from .models import SourceSnapshot
from .synthetic import sync_synthetic_metrics


@staff_member_required
def synthetic_sync(request):
    form = SyntheticSyncForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _, created_count = sync_synthetic_metrics(
            project=form.cleaned_data["project"],
            report_month=form.cleaned_data["report_month"],
            user=request.user,
        )
        if created_count:
            messages.success(request, f"Создано снимков источников: {created_count}.")
        else:
            messages.info(request, "Синтетические снимки за эти периоды уже существуют.")
        return redirect("metrics:synthetic_sync")
    snapshots = SourceSnapshot.objects.select_related("project", "generated_by")[:100]
    return render(
        request,
        "metrics/synthetic_sync.html",
        {"form": form, "snapshots": snapshots},
    )
