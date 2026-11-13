from django.contrib import admin

from .models import Category, Inquiry, Installment, Listing, ListingImage, ListingUnit, Order, OrderEvent, SavedListing


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "group", "slug", "sort_order", "is_active"]
    list_filter = ["group", "is_active"]
    prepopulated_fields = {"slug": ("name",)}


class ListingImageInline(admin.TabularInline):
    model = ListingImage
    extra = 1


class ListingUnitInline(admin.TabularInline):
    model = ListingUnit
    extra = 0
    fields = ["code", "label", "price_override", "status", "held_by", "held_until"]
    readonly_fields = ["held_by", "held_until"]


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ["title", "category", "company", "price", "city", "status", "is_featured", "views"]
    list_filter = ["status", "category__group", "category", "condition", "allow_installments", "is_featured"]
    search_fields = ["title", "brand", "model_name", "company__name", "city", "area"]
    inlines = [ListingImageInline, ListingUnitInline]
    readonly_fields = ["views"]


class InstallmentInline(admin.TabularInline):
    model = Installment
    extra = 0


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    readonly_fields = ["kind", "message", "by", "created_at"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["reference", "user", "listing", "plan", "status", "total_amount", "amount_paid", "created_at"]
    list_filter = ["status", "plan", "funding_source", "listing__category__group"]
    search_fields = ["reference", "user__phone", "user__full_name", "listing__title", "unit__code"]
    readonly_fields = ["reference", "price_breakdown"]
    inlines = [InstallmentInline, OrderEventInline]
    date_hierarchy = "created_at"


admin.site.register(SavedListing)


@admin.register(Inquiry)
class InquiryAdmin(admin.ModelAdmin):
    list_display = ["listing", "user", "kind", "status", "created_at"]
    list_filter = ["kind", "status"]
