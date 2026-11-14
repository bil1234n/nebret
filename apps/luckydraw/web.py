"""
Donations on the website – used by the iPhone app.

Apple only lets approved nonprofits take donations inside an app (App Store guideline 3.2.2(iv)), so on iPhone the
campaign page has a "Donate on the website" button. It asks the API for a signed, short-lived link and opens it in
Safari:

    POST /api/community/crowdfunding/<slug>/web-link/  →  {"url": "https://…/give/<slug>/?t=<signed token>"}
    GET  /give/<slug>/?t=…                              →  amount, message, anonymous – then "Continue to payment"
    POST /give/<slug>/                                  →  creates the donation and redirects to the Chapa checkout
    Chapa → /api/payments/return/<reference>/           →  "Thank you" page with a button back to the app

The token carries the donor's account id, so the gift shows up under their account exactly like an in-app donation.
"""
from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.web import _context
from apps.core.exceptions import BusinessRuleError

from . import services
from .models import CrowdfundingCampaign

SALT = "nebret.web-donation"
PRESETS = [50, 100, 250, 500, 1000, 2500]


def make_token(user, campaign) -> str:
    return signing.dumps({"u": user.pk, "c": campaign.pk}, salt=SALT, compress=True)


def read_token(token: str, campaign):
    """The donor for a valid, unexpired token for this campaign – else ``None``."""
    max_age = settings.NEBRET["WEB_DONATION_LINK_MINUTES"] * 60
    try:
        data = signing.loads(token or "", salt=SALT, max_age=max_age)
    except signing.BadSignature:  # includes SignatureExpired
        return None
    if data.get("c") != campaign.pk:
        return None
    # Suspended or deleted accounts are inactive, so an old link stops working for them.
    return get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()


def web_link(request, user, campaign) -> str:
    return request.build_absolute_uri(f"/give/{campaign.slug}/?t={make_token(user, campaign)}")


class GiveForm(forms.Form):
    t = forms.CharField(widget=forms.HiddenInput)
    amount = forms.IntegerField(min_value=1, max_value=1_000_000, label="Your gift (ETB)")
    message = forms.CharField(required=False, max_length=200, label="Message (optional)",
                              widget=forms.Textarea(attrs={"rows": 2, "placeholder": "A few kind words"}))
    is_anonymous = forms.BooleanField(required=False, label="Give anonymously")


@require_http_methods(["GET", "POST"])
def give(request, slug):
    campaign = get_object_or_404(CrowdfundingCampaign.objects.exclude(status__in=["DRAFT", "CANCELLED"]), slug=slug)
    token = request.POST.get("t") if request.method == "POST" else request.GET.get("t", "")
    user = read_token(token, campaign)
    ctx = _context(campaign=campaign, presets=[p for p in PRESETS if p >= campaign.min_donation],
                   fee_percent=campaign.platform_fee_percent, deep_link=settings.NEBRET["APP_DEEP_LINK"])
    if user is None:
        return render(request, "luckydraw/give.html", {**ctx, "expired": True}, status=403)
    if not campaign.is_live:
        return render(request, "luckydraw/give.html", {**ctx, "closed": True})

    form = GiveForm(request.POST or None, initial={"t": token, "amount": 100 if campaign.min_donation <= 100 else campaign.min_donation})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            _donation, txn = services.start_donation(
                user, campaign, d["amount"], provider=settings.NEBRET["WEB_DONATION_PROVIDER"],
                is_anonymous=d["is_anonymous"], message=d["message"].strip())
        except BusinessRuleError as exc:
            form.add_error(None, str(exc))
        else:
            if txn.checkout_url:
                return redirect(txn.checkout_url)
            form.add_error(None, "The payment page is not available right now. Please try again in a moment.")
    return render(request, "luckydraw/give.html", {**ctx, "form": form, "donor": user})
