from django.db import migrations

TERMS = (
    "Every member pays the same contribution every round and wins the product in a draw – every member wins once.\n"
    "The product price is fixed when the group is created; the seller reserves one unit for every member.\n"
    "The winner's order is created automatically and the product is handed over by the seller.")


def forwards(apps, schema_editor):
    """Product Equbs belong to a "Products" Equb package so they show in the app's Equb tab."""
    EqubPackage = apps.get_model("equb", "EqubPackage")
    EqubGroup = apps.get_model("equb", "EqubGroup")
    pkg = EqubPackage.objects.filter(slug="products").first()
    if pkg is None:
        pkg = EqubPackage.objects.create(name="Products", slug="products", terms_content=TERMS, is_active=True)
    EqubGroup.objects.filter(listing__isnull=False, equb_package__isnull=True).update(equb_package=pkg)


class Migration(migrations.Migration):
    dependencies = [("equb", "0004_product_equb")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
