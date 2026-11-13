"""Create / edit / delete screens for the staff console (replaces the raw Django admin for daily work)."""
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.equb.models import EqubGroup, EqubPackage, EqubSettings
from apps.market.models import Category, Listing

from . import forms as f
from .views import staff_required


def _render(request, *, nav, title, crumbs, form, cancel, delete_url=None, formset=None, intro=None, obj=None):
    return render(request, "console/form.html", {"nav": nav, "title": title, "crumbs": crumbs, "form": form, "formset": formset,
                                                 "cancel": cancel, "delete_url": delete_url, "intro": intro, "obj": obj})


@staff_required
def equb_form(request, slug=None):
    obj = get_object_or_404(EqubGroup, slug=slug) if slug else None
    initial = None
    if not obj:
        initial = {"status": "OPEN", "duration_value": 12, "duration_unit": "MONTHS",
                   "draw_type": "MANUAL", "max_members": 12, "start_date": timezone.localdate() + timedelta(days=7)}
        pkg = EqubPackage.objects.filter(pk=request.GET.get("package") or 0, is_active=True).first()
        if pkg:  # "+ New group" from a package page
            n = pkg.groups.count() + 1
            initial.update(pkg.group_defaults(), equb_package=pkg, name=f"{pkg.name} – Group {n}")
    form = f.EqubForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        from apps.equb import services as equb_services

        group = form.save(commit=False)
        if not obj:
            group.organizer = request.user
        was = obj and EqubGroup.objects.filter(pk=obj.pk).values_list("status", flat=True).first()
        starting = group.status == EqubGroup.Status.ACTIVE and was != EqubGroup.Status.ACTIVE
        if starting:  # "Started" chosen in the form: start it properly (rounds, first round open, members told)
            group.status = EqubGroup.Status.OPEN
        group.save()
        if starting:
            equb_services.start_group(group)
        elif obj and group.status == EqubGroup.Status.ACTIVE:
            equb_services.sync_running_group(group)  # new amount / schedule / rounds / size apply to the running Equb
        messages.success(request, f"Equb group “{group.name}” {'updated' if obj else 'created'}.")
        if not obj and request.POST.get("_another"):
            return redirect(reverse("console:equb_add") + (f"?package={group.equb_package_id}" if group.equb_package_id else ""))
        return redirect("console:equb", slug=group.slug)
    return _render(request, nav="equb", title=f"Edit {obj.name}" if obj else "Create Equb group", crumbs="Equb groups", form=form,
                   cancel=reverse("console:equb", args=[obj.slug]) if obj else reverse("console:equbs"), obj=obj,
                   delete_url=reverse("console:equb_delete", args=[obj.slug]) if obj and not obj.memberships.exists() else None)


@staff_required
@require_POST
def equb_delete(request, slug):
    obj = get_object_or_404(EqubGroup, slug=slug)
    if obj.memberships.exists():
        messages.error(request, "Members have joined this Equb; set it to Cancelled instead.")
        return redirect("console:equb", slug=slug)
    obj.delete()
    messages.success(request, "Equb deleted.")
    return redirect("console:equbs")


@staff_required
def equb_package_form(request, slug=None):
    obj = get_object_or_404(EqubPackage, slug=slug) if slug else None
    form = f.EqubPackageForm(request.POST or None, instance=obj, initial=None if obj else {"is_active": True})
    if request.method == "POST" and form.is_valid():
        pkg = form.save()
        messages.success(request, f"Equb package “{pkg.name}” {'updated' if obj else 'created'}.")
        if not obj and request.POST.get("_another"):
            return redirect("console:equb_package_add")
        return redirect("console:equb_package", slug=pkg.slug)
    return _render(request, nav="equb_packages", title=f"Edit {obj.name}" if obj else "Create Equb package",
                   crumbs="Equb packages", form=form, obj=obj,
                   cancel=reverse("console:equb_package", args=[obj.slug]) if obj else reverse("console:equb_packages"),
                   delete_url=reverse("console:equb_package_delete", args=[obj.slug]) if obj and not obj.groups.exists() else None)


@staff_required
@require_POST
def equb_package_delete(request, slug):
    obj = get_object_or_404(EqubPackage, slug=slug)
    if obj.groups.exists():
        messages.error(request, "This package has groups; make it inactive instead.")
        return redirect("console:equb_package", slug=slug)
    obj.delete()
    messages.success(request, "Equb package deleted.")
    return redirect("console:equb_packages")




@staff_required
def settings_page(request):
    return render(request, "console/settings.html", {
        "nav": "settings",
        "categories": Category.objects.annotate(
            n=Count("listings", distinct=True),
            live=Count("listings", filter=Q(listings__status=Listing.Status.ACTIVE), distinct=True)).order_by(
            "group", "sort_order", "name"),
        "staff": User.objects.filter(is_staff=True).order_by("-is_superuser", "full_name"),
        "equb_rules": EqubSettings.load(),
    })


def _simple(request, form_cls, model, pk, title_new, label):
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = form_cls(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{label} saved.")
        return redirect("console:settings")
    return _render(request, nav="settings", title=f"Edit {obj}" if obj else title_new, crumbs="Settings", form=form,
                   cancel=reverse("console:settings"), obj=obj)


@staff_required
def category_form(request, pk=None):
    return _simple(request, f.CategoryForm, Category, pk, "New marketplace category", "Category")


@staff_required
def staff_form(request):
    if not request.user.is_superuser:
        messages.error(request, "Only full administrators can add staff.")
        return redirect("console:settings")
    form = f.StaffForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = form.save()
        messages.success(request, f"{u.full_name} can now sign in with {u.phone}.")
        return redirect("console:settings")
    return _render(request, nav="settings", title="Add staff member", crumbs="Settings", form=form, cancel=reverse("console:settings"))


@staff_required
@require_POST
def staff_toggle(request, pk):
    u = get_object_or_404(User, pk=pk, is_staff=True)
    if not request.user.is_superuser or u == request.user:
        messages.error(request, "You can't change this account.")
    else:
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        messages.success(request, f"{u.full_name} {'re-activated' if u.is_active else 'deactivated'}.")
    return redirect("console:settings")


# --------------------------------------------------------------------------- mobile app: banners & updates
@staff_required
def app_content(request):
    from apps.core.models import AppRelease, Banner

    return render(request, "console/app_content.html", {
        "nav": "app_content", "banners": Banner.objects.all(), "releases": AppRelease.objects.all()[:30],
        "now": timezone.now()})


@staff_required
def banner_form(request, pk=None):
    from apps.core.models import Banner

    obj = get_object_or_404(Banner, pk=pk) if pk else None
    form = f.BannerForm(request.POST or None, request.FILES or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Banner {'updated' if obj else 'added'}.")
        return redirect("console:app_content")
    return _render(request, nav="app_content", title="Edit banner" if obj else "New home banner", crumbs="Mobile app",
                   form=form, cancel=reverse("console:app_content"), obj=obj,
                   delete_url=reverse("console:banner_delete", args=[obj.pk]) if obj else None)


@staff_required
@require_POST
def banner_delete(request, pk):
    from apps.core.models import Banner

    get_object_or_404(Banner, pk=pk).delete()
    messages.success(request, "Banner deleted.")
    return redirect("console:app_content")


@staff_required
def release_form(request, pk=None):
    from apps.core.models import AppRelease

    obj = get_object_or_404(AppRelease, pk=pk) if pk else None
    form = f.AppReleaseForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        rel = form.save()
        messages.success(request, f"{rel} saved – older apps now show the "
                                  f"{'forced' if rel.kind == 'FORCE' else 'optional'} update popup.")
        return redirect("console:app_content")
    return _render(request, nav="app_content", title="Edit app update" if obj else "New app update", crumbs="Mobile app",
                   form=form, cancel=reverse("console:app_content"), obj=obj,
                   delete_url=reverse("console:release_delete", args=[obj.pk]) if obj else None)


@staff_required
@require_POST
def release_delete(request, pk):
    from apps.core.models import AppRelease

    get_object_or_404(AppRelease, pk=pk).delete()
    messages.success(request, "App update removed.")
    return redirect("console:app_content")
