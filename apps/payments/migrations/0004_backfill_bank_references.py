"""
Register the transfer references of existing office bank / mobile-money payments, so a new payment can't reuse one.
References that were already used twice before this rule existed are kept as they are and raised as fraud alerts.
"""
import re

from django.db import migrations

NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def backfill(apps, schema_editor):
    PaymentTransaction = apps.get_model("payments", "PaymentTransaction")
    BankReference = apps.get_model("payments", "BankReference")
    FraudAlert = apps.get_model("payments", "FraudAlert")
    seen = set(BankReference.objects.values_list("bank", "reference"))
    qs = PaymentTransaction.objects.filter(provider__in=["bank", "mobile"], status__in=["SUCCESS", "REFUNDED"])
    for txn in qs.order_by("created_at", "pk").iterator():
        ref = NOT_ALNUM.sub("", (txn.provider_reference or "").upper())[:64]
        if not ref:
            continue
        bank = "MOBILE" if txn.provider == "mobile" else "BANK"
        if (bank, ref) in seen:
            first = BankReference.objects.filter(bank=bank, reference=ref).first()
            FraudAlert.objects.create(
                kind="DUPLICATE_REFERENCE", message=f"{bank} reference {ref} was used more than once before "
                                                    f"duplicate checks existed – please review."[:255],
                user_id=txn.recorded_by_id, agency_id=txn.agency_id, existing=first,
                data={"bank": bank, "reference": ref, "amount": str(txn.amount), "payment": str(txn.reference),
                      "context": "backfill"})
            continue
        BankReference.objects.create(bank=bank, reference=ref, payment_id=txn.pk, source="backfill")
        seen.add((bank, ref))


class Migration(migrations.Migration):
    dependencies = [("payments", "0003_bank_reference_fraud_alert")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
