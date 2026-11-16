"""Forms for the company portal and for managing partner companies in the Nebret console."""
from __future__ import annotations

import json

from django import forms
from django.utils import timezone

from django.forms.models import ModelChoiceIterator

from apps.console.forms import StyledMixin
from apps.market.models import Category, Listing, Order
from apps.market.uploads import UploadError, upload_image

from .models import Agency, AgencyStaff, FamilyMember


class AgencyForm(StyledMixin, forms.ModelForm):
    """Console: create / edit a partner company. On create, the owner's account is set up too."""

    owner_name = forms.CharField(label="Owner full name", max_length=150, required=False)
    owner_phone = forms.CharField(label="Owner phone", max_length=20, required=False,
                                  help_text="The owner signs in to the company portal with this number.")
    owner_password = forms.CharField(label="Owner PIN (6 digits)", required=False, min_length=6, max_length=6,
                                     widget=forms.TextInput(attrs={"inputmode": "numeric", "maxlength": 6, "pattern": "[0-9]{6}", "autocomplete": "off"}),
                                     help_text="The owner signs in to the app and the company portal with their phone and this PIN.")
    bank_code = forms.TypedChoiceField(label="Bank", coerce=int, required=False, empty_value=None)

    sections = [
        ("Company", "", ["name", "kind", "license_number", "tin", "phone", "email", "website", "city", "address"]),
        ("Public profile", "What customers see on the company page in the app.",
         ["description", "logo_url", "is_verified"]),
        ("Owner account", "The person who runs the company account. They can add their own sales team later.",
         ["owner_name", "owner_phone", "owner_password"]),
        ("Commission & status", "Nebret's commission is taken from every sale the company makes.",
         ["commission_percent", "status"]),
        ("Payout bank account", "Chapa sends the company's share of every sale here automatically, once the Chapa "
                                "subaccount is created on the company's page.",
         ["bank_code", "account_name", "account_number"]),
        ("Internal", "", ["notes"]),
    ]

    class Meta:
        model = Agency
        fields = ["name", "kind", "license_number", "tin", "phone", "email", "website", "city", "address",
                  "description", "logo_url", "is_verified", "commission_percent", "status", "bank_code",
                  "account_name", "account_number", "notes"]
        labels = {"commission_percent": "Nebret commission (%)", "account_name": "Account holder name",
                  "is_verified": "Verified seller"}
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, banks=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.banks = {b["id"]: b["name"] for b in (banks or [])}
        choices = [("", "— choose the bank —")] + [(b["id"], b["name"]) for b in (banks or [])]
        if self.instance.bank_code and self.instance.bank_code not in self.banks:
            choices.append((self.instance.bank_code, self.instance.bank_name or f"Bank #{self.instance.bank_code}"))
        self.fields["bank_code"].choices = choices
        if not banks:
            self.fields["bank_code"].help_text = "Chapa's bank list couldn't be loaded – check the Chapa keys."
        if self.instance.pk:
            for n in ("owner_name", "owner_phone", "owner_password"):
                self.fields.pop(n)
            self.sections = [s for s in self.sections if s[0] != "Owner account"]
            if self.instance.chapa_subaccount_id:
                self.disable("bank_code", "account_name", "account_number",
                             reason="Linked to Chapa. To change it, remove the subaccount in Chapa first.")
        else:
            self.fields["owner_name"].required = self.fields["owner_phone"].required = True

    def save(self, commit=True):
        obj = super().save(commit=False)
        if obj.bank_code:
            obj.bank_name = self.banks.get(obj.bank_code, obj.bank_name)
        if commit:
            obj.save()
        return obj


class CompanyProfileForm(StyledMixin, forms.ModelForm):
    """Company portal: what the company may change about itself."""

    sections = [("Public profile", "Shown to customers in the app.",
                 ["description", "logo_url", "website", "phone", "email", "address"])]

    class Meta:
        model = Agency
        fields = ["description", "logo_url", "website", "phone", "email", "address"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class JSONListField(forms.CharField):
    """One item per line ⇄ JSON list."""

    widget = forms.Textarea(attrs={"rows": 4})

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return "\n".join(str(v) for v in value)
        return value

    def to_python(self, value):
        return [line.strip() for line in (value or "").splitlines() if line.strip()]


class SpecsField(forms.CharField):
    """``key: value`` per line ⇄ JSON object (numbers stay numbers)."""

    widget = forms.Textarea(attrs={"rows": 5, "placeholder": "bedrooms: 3\narea_m2: 145\nfloor: 4"})

    def prepare_value(self, value):
        if isinstance(value, dict):
            return "\n".join(f"{k}: {v}" for k, v in value.items())
        return value

    def to_python(self, value):
        out = {}
        for line in (value or "").splitlines():
            if ":" not in line:
                continue
            k, v = (p.strip() for p in line.split(":", 1))
            if not k:
                continue
            key = k.lower().replace(" ", "_")
            try:
                out[key] = json.loads(v)
            except ValueError:
                out[key] = v
        return out


NEW_CATEGORY = "__new__"


class _CategoryIterator(ModelChoiceIterator):
    """The category list plus a last "Other" choice for typing a new category."""

    def __iter__(self):
        yield from super().__iter__()
        if getattr(self.field, "allow_new", False):
            yield (NEW_CATEGORY, "Other – add my own category…")

    def __len__(self):
        return super().__len__() + (1 if getattr(self.field, "allow_new", False) else 0)


class CategoryChoiceField(forms.ModelChoiceField):
    iterator = _CategoryIterator
    allow_new = False

    def to_python(self, value):
        if value == NEW_CATEGORY and self.allow_new:
            return NEW_CATEGORY
        return super().to_python(value)


class MultipleImageInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.ImageField):
    """Several pictures from one file input (Django checks each one is a real image)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleImageInput(attrs={"accept": "image/*", "multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        return [super(MultipleImageField, self).clean(f, initial) for f in files if f]


class ListingForm(StyledMixin, forms.ModelForm):
    """Create / edit a listing (company portal and console)."""

    features = JSONListField(required=False, help_text="One per line, e.g. Parking, Elevator, 24/7 security.")
    specs = SpecsField(required=False, help_text="One fact per line as “name: value”. The category's spec names are "
                                                 "shown first in the app.")
    initial_units = forms.IntegerField(label="How many units for sale", min_value=0, max_value=1000, required=False,
                                       initial=1, help_text="Each unit is one sellable item (apartment, car, e-bike…). "
                                                            "You can add or name units later.")
    gallery = JSONListField(label="More photos (links)", required=False,
                            help_text="One picture link per line. Pictures you upload above are added here.")
    image_file = forms.ImageField(label="Cover picture", required=False,
                                  widget=forms.ClearableFileInput(attrs={"accept": "image/*", "data-upload-preview": "1"}),
                                  help_text="Upload from your computer or phone – or paste a link below.")
    gallery_files = MultipleImageField(label="More photos", required=False,
                                       help_text="Choose one or several pictures at once.")
    new_category = forms.CharField(label="New category name", max_length=80, required=False,
                                   help_text="It is added to the category list and can be edited later.")
    new_category_group = forms.ChoiceField(label="New category type", required=False, choices=Category.Group.choices)

    sections = [
        ("What you are selling", "", ["category", "new_category", "new_category_group", "title", "summary",
                                      "description", "brand", "model_name", "year", "condition"]),
        ("Location", "", ["city", "area", "address"]),
        ("Details", "", ["specs", "features", "image_file", "image_url", "gallery_files", "gallery", "video_url"]),
        ("Price", "", ["price", "compare_at_price", "initial_units"]),
        ("How customers can pay", "Offer at least one. Installments use a flat, fixed finance charge (0 = "
                                  "interest-free).",
         ["allow_full_payment", "allow_reservation", "reservation_fee", "reservation_days", "allow_installments",
          "min_down_payment_percent", "max_installment_months", "finance_rate_percent"]),
        ("Handover", "", ["delivery_available", "delivery_fee", "handover_days", "warranty_months"]),
        ("Publishing", "", ["status", "is_featured"]),
    ]

    class Meta:
        model = Listing
        fields = ["category", "title", "summary", "description", "brand", "model_name", "year", "condition", "city",
                  "area", "address", "specs", "features", "image_url", "video_url", "price", "compare_at_price",
                  "allow_full_payment", "allow_reservation", "reservation_fee", "reservation_days",
                  "allow_installments", "min_down_payment_percent", "max_installment_months", "finance_rate_percent",
                  "delivery_available", "delivery_fee", "handover_days", "warranty_months",
                  "status", "is_featured"]
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}
        labels = {"image_url": "Cover picture link"}

    def __init__(self, *args, company_portal=False, allow_new_category=False, **kwargs):
        super().__init__(*args, **kwargs)
        old = self.fields["category"]
        cat = CategoryChoiceField(queryset=old.queryset, required=True, label=old.label, help_text=old.help_text,
                                  initial=old.initial)
        cat.allow_new = allow_new_category
        self.fields["category"] = cat
        if allow_new_category:
            cat.widget.attrs["data-new-category"] = NEW_CATEGORY
        else:
            for n in ("new_category", "new_category_group"):
                self.fields.pop(n)
            self.sections = [(t, h, [n for n in names if n not in ("new_category", "new_category_group")])
                             for t, h, names in self.sections]
        self.fields["image_url"].help_text = "Filled in automatically when you upload a picture."
        if self.instance.pk:
            self.fields.pop("initial_units")
            self.sections = [(t, h, [n for n in names if n != "initial_units"]) for t, h, names in self.sections]
            self.fields["gallery"].initial = [i.image_url for i in self.instance.images.all() if i.image_url]
        if company_portal:
            self.fields.pop("is_featured")
            self.sections = [(t, h, [n for n in names if n != "is_featured"]) for t, h, names in self.sections]

    def clean(self):
        data = super().clean()
        new_cat = data.get("category") == NEW_CATEGORY
        if new_cat:
            if not (data.get("new_category") or "").strip():
                self.add_error("new_category", "Type the name of the new category.")
            if not data.get("new_category_group"):
                self.add_error("new_category_group", "Choose what kind of item this category is.")
        if self.errors:
            # never hand "__new__" to the model, and only upload once everything else is right
            if new_cat:
                data.pop("category", None)
            return data
        try:
            if data.get("image_file"):
                data["image_url"] = upload_image(data["image_file"])
            uploaded = [upload_image(f) for f in data.get("gallery_files") or []]
        except UploadError as exc:
            self.add_error(None, str(exc))
            if new_cat:
                data.pop("category", None)
            return data
        if uploaded:
            have = list(data.get("gallery") or [])
            data["gallery"] = have + [u for u in uploaded if u not in have]
            if not data.get("image_url"):
                data["image_url"] = data["gallery"][0]
        if new_cat:
            name = data["new_category"].strip()
            category = Category.objects.filter(name__iexact=name).first() or Category.objects.create(
                name=name, group=data["new_category_group"],
                sort_order=(Category.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1)
            data["category"] = category
        return data

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        if self.data.get(self.add_prefix("category")) == NEW_CATEGORY:
            exclude.add("category")  # the new category is created in clean(); don't report it as missing
        return exclude

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            self.save_gallery(obj)
        return obj

    def save_gallery(self, obj):
        from apps.market.models import ListingImage

        urls = self.cleaned_data.get("gallery") or []
        obj.images.filter(image="").exclude(image_url__in=urls).delete()
        have = set(obj.images.values_list("image_url", flat=True))
        ListingImage.objects.bulk_create([ListingImage(listing=obj, image_url=u, sort_order=i)
                                          for i, u in enumerate(urls) if u not in have])


class UnitsForm(StyledMixin, forms.Form):
    count = forms.IntegerField(label="Add units", min_value=1, max_value=500, initial=1)
    prefix = forms.CharField(label="Code prefix", max_length=12, required=False,
                             help_text="e.g. BLK-A, VIN or EBK. Codes are numbered automatically.")
    sections = [("Add stock", "", ["count", "prefix"])]


class SaleForm(StyledMixin, forms.Form):
    """Company portal / console: place an order for a customer."""

    listing = forms.ModelChoiceField(queryset=Listing.objects.none())
    plan = forms.ChoiceField(choices=Order.Plan.choices)
    months = forms.IntegerField(label="Installment months", min_value=1, max_value=120, required=False)
    down_payment = forms.DecimalField(max_digits=14, decimal_places=2, required=False,
                                      help_text="Installments only. Empty = the minimum down payment.")
    delivery_method = forms.ChoiceField(choices=Order.Delivery.choices, initial=Order.Delivery.PICKUP)
    delivery_address = forms.CharField(max_length=255, required=False)
    buyer_name = forms.CharField(label="Name on the contract / title", max_length=120, required=False)
    buyer_id_number = forms.CharField(label="National ID / Kebele ID", max_length=40, required=False)
    sections = [("Item & payment", "", ["listing", "plan", "months", "down_payment"]),
                ("Handover & buyer", "", ["delivery_method", "delivery_address", "buyer_name", "buyer_id_number"])]

    def __init__(self, *args, listings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["listing"].queryset = listings if listings is not None else Listing.objects.live()


class StaffForm(StyledMixin, forms.Form):
    full_name = forms.CharField(max_length=150)
    phone = forms.CharField(max_length=20, help_text="They sign in with this phone number.")
    password = forms.CharField(label="PIN (6 digits)", min_length=6, max_length=6, required=False, widget=forms.TextInput(attrs={"inputmode": "numeric", "maxlength": 6, "pattern": "[0-9]{6}", "autocomplete": "off"}),
                               help_text="They sign in with their phone and this PIN. Leave empty if they already have an account.")
    role = forms.ChoiceField(choices=[(AgencyStaff.Role.AGENT, "Sales agent"), (AgencyStaff.Role.MANAGER, "Manager")])
    sections = [("Team member", "", ["full_name", "phone", "password", "role"])]


class CustomerForm(StyledMixin, forms.Form):
    full_name = forms.CharField(max_length=150, help_text="As written on their national ID.")
    phone = forms.CharField(max_length=20, help_text="Their own mobile number, e.g. 0911 234 567.")
    email = forms.EmailField(required=False)
    password = forms.CharField(label="App PIN (6 digits)", required=False, min_length=6, max_length=6, widget=forms.TextInput(attrs={"inputmode": "numeric", "maxlength": 6, "pattern": "[0-9]{6}", "autocomplete": "off"}),
                               help_text="Optional – lets them sign in to the Nebret app with their phone and this PIN.")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    sections = [("Customer", "", ["full_name", "phone", "email", "password", "notes"])]


class FamilyForm(StyledMixin, forms.ModelForm):
    sections = [("Family member", "A co-buyer or beneficiary on an order, or an Equb place that the customer pays for.",
                 ["first_name", "last_name", "relation", "gender", "date_of_birth", "phone", "national_id"])]

    class Meta:
        model = FamilyMember
        fields = ["first_name", "last_name", "relation", "gender", "date_of_birth", "phone", "national_id"]

    def clean_date_of_birth(self):
        d = self.cleaned_data["date_of_birth"]
        if d > timezone.localdate():
            raise forms.ValidationError("The date of birth can't be in the future.")
        return d
