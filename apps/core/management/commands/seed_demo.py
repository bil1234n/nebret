"""
Load realistic demo data: marketplace categories, partner companies (real estate, cars, e-bikes, electronics, land),
listings with stock and payment plans, property Equbs, a crowdfunding campaign and a giveaway.

    python manage.py seed_demo

Safe to run more than once (it updates what is already there).
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

CATEGORIES = [
    # group, name, Amharic, icon, spec fields
    ("VEHICLES", "Cars", "መኪናዎች", "car-sport", [
        {"key": "mileage_km", "label": "Mileage", "unit": "km"}, {"key": "fuel", "label": "Fuel"},
        {"key": "transmission", "label": "Transmission"}, {"key": "engine", "label": "Engine"},
        {"key": "seats", "label": "Seats"}, {"key": "colour", "label": "Colour"}]),
    ("VEHICLES", "Trucks & commercial", "የጭነት መኪናዎች", "bus", [
        {"key": "capacity_t", "label": "Load capacity", "unit": "t"}, {"key": "fuel", "label": "Fuel"},
        {"key": "mileage_km", "label": "Mileage", "unit": "km"}]),
    ("HOMES", "Apartments", "አፓርትመንቶች", "business", [
        {"key": "bedrooms", "label": "Bedrooms"}, {"key": "bathrooms", "label": "Bathrooms"},
        {"key": "area_m2", "label": "Area", "unit": "m²"}, {"key": "floor", "label": "Floor"},
        {"key": "parking", "label": "Parking"}, {"key": "completion", "label": "Completion"}]),
    ("HOMES", "Houses & villas", "ቤቶች እና ቪላዎች", "home", [
        {"key": "bedrooms", "label": "Bedrooms"}, {"key": "bathrooms", "label": "Bathrooms"},
        {"key": "plot_m2", "label": "Plot", "unit": "m²"}, {"key": "built_m2", "label": "Built area", "unit": "m²"}]),
    ("LAND", "Residential plots", "የመኖሪያ ቦታ", "map", [
        {"key": "plot_m2", "label": "Plot size", "unit": "m²"}, {"key": "title_deed", "label": "Title deed"},
        {"key": "road_access", "label": "Road access"}]),
    ("LAND", "Shops & offices", "ሱቆች እና ቢሮዎች", "storefront", [
        {"key": "area_m2", "label": "Area", "unit": "m²"}, {"key": "floor", "label": "Floor"},
        {"key": "use", "label": "Use"}]),
    ("ELECTRONICS", "E-bikes & motorbikes", "ኤሌክትሪክ ብስክሌቶች", "bicycle", [
        {"key": "range_km", "label": "Range", "unit": "km"}, {"key": "battery", "label": "Battery"},
        {"key": "top_speed_kmh", "label": "Top speed", "unit": "km/h"}, {"key": "charge_h", "label": "Charging",
                                                                         "unit": "h"}]),
    ("ELECTRONICS", "Phones, TVs & appliances", "ስልኮች እና የቤት እቃዎች", "tv", [
        {"key": "brand", "label": "Brand"}, {"key": "storage", "label": "Storage"},
        {"key": "screen", "label": "Screen"}, {"key": "warranty", "label": "Warranty"}]),
]

COMPANIES = [
    # name, kind, phone, owner name, owner phone, city, description
    ("Sunshine Real Estate", "REAL_ESTATE", "+251116610000", "Abel Tesfaye", "0911000101", "Addis Ababa",
     "Developer of modern apartment blocks in Bole, CMC and Ayat since 2008."),
    ("Moenco Motors", "CAR_DEALER", "+251115510000", "Hana Girma", "0911000102", "Addis Ababa",
     "Authorised dealer for new Toyota and Suzuki vehicles, with service and genuine parts."),
    ("Green Wheel E-Mobility", "EBIKE", "+251116620000", "Dawit Alemu", "0911000103", "Addis Ababa",
     "Electric bikes and scooters for commuters and delivery riders, assembled in Ethiopia."),
    ("Bole Home Electronics", "ELECTRONICS", "+251116630000", "Sara Mekonnen", "0911000104", "Addis Ababa",
     "Phones, TVs, fridges and washing machines with official warranty."),
    ("Legetafo Land Partners", "LAND", "+251116640000", "Yonas Bekele", "0911000105", "Legetafo",
     "Serviced residential plots with title deeds just outside Addis Ababa."),
]

IMG = "https://images.unsplash.com/photo-{}?w=1200&q=70"

LISTINGS = [
    # company index, category, title, brand, model, year, condition, city, area, price, units, specs, features, image,
    # plans: (full, reserve_fee, installments(months, down%, rate), equb), delivery fee, handover days, warranty
    (0, "Apartments", "3 bedroom apartment, Bole Atlas", "Sunshine", "Atlas Residence", 2026, "OFF_PLAN",
     "Addis Ababa", "Bole", 7800000, 12, {"bedrooms": 3, "bathrooms": 2, "area_m2": 145, "floor": "2–12",
                                           "parking": "1 space", "completion": "Q4 2027"},
     ["Elevator", "Backup generator", "24/7 security", "Underground parking"], "1545324418-cc1a3fa10c00",
     (True, 100000, (36, 30, 0), True), 0, 30, 0),
    (0, "Apartments", "2 bedroom apartment, CMC", "Sunshine", "CMC Gardens", 2025, "NEW", "Addis Ababa", "CMC",
     5200000, 8, {"bedrooms": 2, "bathrooms": 2, "area_m2": 98, "floor": "3–9", "parking": "Shared"},
     ["Elevator", "Children's playground", "Water reservoir"], "1502672260266-1c1ef2d93688",
     (True, 75000, (24, 35, 0), True), 0, 14, 0),
    (0, "Houses & villas", "4 bedroom G+1 villa, Ayat", "Sunshine", "Ayat Villas", 2025, "NEW", "Addis Ababa", "Ayat",
     18500000, 3, {"bedrooms": 4, "bathrooms": 3, "plot_m2": 250, "built_m2": 320},
     ["Private garden", "Servant quarter", "2 car garage"], "1568605114967-8130f3a36994",
     (True, 250000, (48, 40, 6), True), 0, 30, 0),
    (1, "Cars", "Toyota Corolla Cross Hybrid", "Toyota", "Corolla Cross", 2026, "NEW", "Addis Ababa", "Kazanchis",
     4650000, 6, {"mileage_km": 0, "fuel": "Hybrid", "transmission": "Automatic", "engine": "1.8 L", "seats": 5,
                  "colour": "Pearl white"},
     ["Reverse camera", "Lane assist", "3 years free service"], "1621007947382-bb3c3994e3fb",
     (True, 50000, (24, 40, 8), True), 15000, 7, 36),
    (1, "Cars", "Suzuki Dzire 1.2 AT", "Suzuki", "Dzire", 2026, "NEW", "Addis Ababa", "Kazanchis", 2350000, 10,
     {"mileage_km": 0, "fuel": "Petrol", "transmission": "Automatic", "engine": "1.2 L", "seats": 5,
      "colour": "Silver"}, ["Air conditioning", "Touchscreen", "Fuel efficient"], "1549317661-bd32c8ce0db2",
     (True, 30000, (18, 35, 8), True), 10000, 5, 24),
    (1, "Trucks & commercial", "Isuzu NPR 5-ton truck", "Isuzu", "NPR", 2025, "NEW", "Addis Ababa", "Kality",
     6900000, 4, {"capacity_t": 5, "fuel": "Diesel", "mileage_km": 0}, ["Power steering", "Cargo box option"],
     "1601584115197-04ecc0da31d7", (True, 100000, (24, 40, 10), True), 0, 10, 24),
    (2, "E-bikes & motorbikes", "Green Wheel Commuter e-bike", "Green Wheel", "Commuter 48V", 2026, "NEW",
     "Addis Ababa", "Megenagna", 68000, 40, {"range_km": 70, "battery": "48V 20Ah lithium", "top_speed_kmh": 35,
                                              "charge_h": 5},
     ["Removable battery", "Disc brakes", "LED lights"], "1571068316344-75bc76f77890",
     (True, 5000, (12, 20, 0), True), 1500, 3, 12),
    (2, "E-bikes & motorbikes", "Cargo e-scooter for delivery", "Green Wheel", "Cargo Pro", 2026, "NEW",
     "Addis Ababa", "Megenagna", 145000, 15, {"range_km": 90, "battery": "60V 32Ah lithium", "top_speed_kmh": 45,
                                              "charge_h": 6},
     ["Rear cargo box", "Phone holder", "Anti-theft alarm"], "1558981403-c5f9899a28bc",
     (True, 10000, (12, 25, 0), True), 2000, 3, 12),
    (3, "Phones, TVs & appliances", "Samsung Galaxy A56 5G 256GB", "Samsung", "Galaxy A56", 2026, "NEW",
     "Addis Ababa", "Bole", 52000, 30, {"brand": "Samsung", "storage": "256 GB", "screen": "6.7 inch",
                                         "warranty": "1 year"},
     ["Official warranty", "Free screen protector"], "1610945265064-0e34e5519bbf", (True, 0, (6, 30, 0), True), 0, 1, 12),
    (3, "Phones, TVs & appliances", "LG 65\" 4K Smart TV", "LG", "UR8050", 2025, "NEW", "Addis Ababa", "Bole",
     98000, 12, {"brand": "LG", "screen": "65 inch 4K", "warranty": "2 years"}, ["Wall mount included", "Free delivery"],
     "1593359677879-a4bb92f829d1", (True, 0, (10, 30, 0), True), 0, 2, 24),
    (4, "Residential plots", "300 m² residential plot, Legetafo", "Legetafo Land", "Phase 2", None, "NEW", "Legetafo",
     "Phase 2", 3600000, 20, {"plot_m2": 300, "title_deed": "Yes – lease", "road_access": "Asphalt, 200 m"},
     ["Water & power to the plot", "Fenced compound"], "1500382017468-9049fed747ef",
     (True, 60000, (24, 30, 0), True), 0, 21, 0),
    (0, "Shops & offices", "Ground-floor shop, Bole Atlas", "Sunshine", "Atlas Residence", 2026, "OFF_PLAN",
     "Addis Ababa", "Bole", 9500000, 4, {"area_m2": 80, "floor": "Ground", "use": "Retail"},
     ["Street front", "Shared parking"], "1497366216548-37526070297c", (True, 150000, (36, 40, 6), True), 0, 30, 0),
]


def _summary(category, specs: dict) -> str:
    """ "3 Bedrooms · 145 m² Area" – the first facts of the listing, in the order its category shows them."""
    parts = []
    for f in category.spec_fields or []:
        v = specs.get(f.get("key"))
        if v in (None, "", 0):
            continue
        unit = f.get("unit", "")
        parts.append(f"{v} {unit}".strip() if unit else (f"{v} {f['label'].lower()}" if isinstance(v, (int, float)) else str(v)))
        if len(parts) == 3:
            break
    return " · ".join(parts) or category.name


class Command(BaseCommand):
    help = "Seed demo data for local development and app demos"

    @transaction.atomic
    def handle(self, *args, **opts):
        from apps.accounts.models import User
        from apps.agencies.models import Agency, AgencyStaff
        from apps.agencies.services import add_staff
        from apps.equb.models import EqubGroup, EqubPackage
        from apps.equb.services import join_group
        from apps.luckydraw.models import CrowdfundingCampaign, Giveaway
        from apps.market.models import Category, Listing, ListingImage

        now, today = timezone.now(), timezone.localdate()

        admin = User.objects.filter(phone="+251900000000").first()
        if not admin:
            admin = User.objects.create_superuser("+251900000000", "admin12345", full_name="Nebret Admin")
        customer = User.objects.filter(phone="+251911223344").first()
        if not customer:
            customer = User.objects.create_user("0911223344", "demo12345", full_name="Meron Tadesse",
                                                email="meron@example.com", city="Addis Ababa")

        cats = {}
        for i, (group, name, am, icon, fields) in enumerate(CATEGORIES):
            cats[name], _ = Category.objects.update_or_create(
                name=name, defaults=dict(group=group, name_am=am, icon=icon, spec_fields=fields, sort_order=i))

        companies = []
        for name, kind, phone, owner, owner_phone, city, desc in COMPANIES:
            c, created = Agency.objects.update_or_create(
                name=name, defaults=dict(kind=kind, phone=phone, city=city, description=desc, is_verified=True,
                                         status=Agency.Status.ACTIVE, created_by=admin))
            if not AgencyStaff.objects.filter(agency=c, role=AgencyStaff.Role.OWNER).exists():
                add_staff(c, full_name=owner, phone=owner_phone, password="company123", role=AgencyStaff.Role.OWNER)
            companies.append(c)

        made = 0
        for (ci, cat, title, brand, model, year, cond, city, area, price, units, specs, features, img, plans,
             delivery_fee, handover, warranty) in LISTINGS:
            full, reserve_fee, (months, down, rate), equb = plans
            listing, created = Listing.objects.update_or_create(
                title=title, company=companies[ci], defaults=dict(
                    category=cats[cat], brand=brand, model_name=model, year=year, condition=cond, city=city, area=area,
                    summary=_summary(cats[cat], specs), price=Decimal(price),
                    description=f"{title} from {companies[ci].name}. " + " · ".join(features),
                    specs=specs, features=features, image_url=IMG.format(img), allow_full_payment=full,
                    allow_reservation=bool(reserve_fee), reservation_fee=Decimal(reserve_fee), reservation_days=14,
                    allow_installments=bool(months), max_installment_months=months or 1,
                    min_down_payment_percent=Decimal(down), finance_rate_percent=Decimal(rate),
                    allow_equb_payout=equb, delivery_available=bool(delivery_fee), delivery_fee=Decimal(delivery_fee),
                    handover_days=handover, warranty_months=warranty, status=Listing.Status.ACTIVE,
                    is_featured=made < 6, created_by=admin))
            if created:
                listing.add_units(units)
                ListingImage.objects.create(listing=listing, image_url=IMG.format(img), sort_order=0)
            made += 1

        package, _ = EqubPackage.objects.get_or_create(
            name="Property Equb", defaults={"terms_content": (
                "# Property Equb agreement\n## How it works\n1. Every member pays the same amount every round.\n"
                "2. Each round's fair draw decides who receives the payout first – everyone receives it once.\n"
                "3. The payout can be spent on any listing in the Nebret marketplace, or taken in cash.\n"
                "!! Nobody can lose money: the draw only decides the order.")})
        equbs = [
            ("E-bike Equb · 12 weeks", Decimal("6000"), 12, "WEEKS", 12),
            ("Car Equb · 24 months", Decimal("100000"), 24, "MONTHS", 24),
            ("Apartment Equb · 36 months", Decimal("250000"), 36, "MONTHS", 36),
        ]
        for name, amount, value, unit, members in equbs:
            g, created = EqubGroup.objects.get_or_create(name=name, defaults=dict(
                equb_package=package, contribution_amount=amount, duration_value=value, duration_unit=unit,
                max_members=members, start_date=today + timedelta(days=7), status=EqubGroup.Status.OPEN,
                draw_type=EqubGroup.DrawType.AUTOMATIC, rules_text=package.terms_content))
            if created:
                join_group(customer, g, notify_member=False)

        CrowdfundingCampaign.objects.get_or_create(title="A new home for the Abebe family", defaults=dict(
            category=CrowdfundingCampaign.Category.HOME, beneficiary_name="Abebe family",
            story="The family lost their home in a fire. Your gift helps them pay the down payment on a small apartment.",
            goal_amount=Decimal("1500000"), image_url=IMG.format("1560518883-ce09059eeffa"), is_featured=True,
            created_by=admin))
        ebike = Listing.objects.filter(title="Green Wheel Commuter e-bike").first()
        Giveaway.objects.get_or_create(title="Win a Green Wheel e-bike", defaults=dict(
            description="Invite 10 friends who register with your code to earn a free entry. Paid by Nebret Equb.",
            prize_type=Giveaway.PrizeType.LISTING, prize_title="Green Wheel Commuter e-bike",
            prize_value=Decimal("68000"), prize_listing=ebike, number_of_winners=1, starts_at=now,
            ends_at=now + timedelta(days=30), draw_at=now + timedelta(days=31), is_featured=True,
            image_url=IMG.format("1571068316344-75bc76f77890")))

        self.stdout.write(self.style.SUCCESS(
            f"Demo data ready: {len(cats)} categories, {len(companies)} companies, {made} listings, {len(equbs)} Equbs.\n"
            "  Admin:    +251900000000 / admin12345  (console: /console/)\n"
            "  Customer: 0911223344 / demo12345     (mobile app)\n"
            "  Company:  0911000101 / company123    (company portal: /company/ – Sunshine Real Estate)"))
