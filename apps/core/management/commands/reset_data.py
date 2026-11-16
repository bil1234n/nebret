"""
Wipe demo / test data to start clean.

    python manage.py reset_data              # asks for confirmation
    python manage.py reset_data --yes        # no prompt
    python manage.py reset_data --keep-catalog   # remove customers & transactions only,
                                                 # keep categories, listings (stock put back on sale), companies,
                                                 # Equb pools, campaigns and giveaways

Always kept: staff accounts (so you can still sign in) and marketplace categories.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Delete all customers, orders, payments, Equb/crowdfunding/giveaway activity (and the catalogue) for a clean start."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
        parser.add_argument("--keep-catalog", action="store_true",
                            help="Keep listings, companies, Equb pools, campaigns and giveaways (reset their counters)")

    def handle(self, *args, **o):
        from apps.accounts.models import User
        from apps.agencies.models import Agency, AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember
        from apps.equb.models import (CallLog, Contribution, EqubDraw, EqubGroup, EqubMembership, EqubPackage,
                                      EqubPayment, EqubPayout, EqubRound, GroupEqub, GroupEqubInvite, JoinRequest,
                                      ScheduledPayment, Termination)
        from apps.luckydraw.models import (CrowdfundingCampaign, Disbursement, Donation, Giveaway, GiveawayEntry,
                                           GiveawayParticipant, GiveawayWinner)
        from apps.market.models import (Inquiry, Installment, Listing, ListingImage, ListingUnit, Order, OrderEvent,
                                        SavedListing)
        from apps.notifications.models import Notification
        from apps.payments.models import PaymentTransaction, WebhookEvent

        keep = o["keep_catalog"]
        customers = User.objects.filter(is_staff=False, agency_staff__isnull=True) if keep else User.objects.filter(
            is_staff=False)
        summary = {
            "customers": customers.count(), "orders": Order.objects.count(), "payments": PaymentTransaction.objects.count(),
            "equb memberships": EqubMembership.objects.count(), "crowdfunding donations": Donation.objects.count(),
        }
        if not keep:
            summary |= {"listings": Listing.objects.count(), "partner companies": Agency.objects.count(),
                        "Equb groups": EqubGroup.objects.count(),
                        "crowdfunding campaigns": CrowdfundingCampaign.objects.count(),
                        "giveaways": Giveaway.objects.count()}
        self.stdout.write("This will permanently delete:")
        for k, v in summary.items():
            self.stdout.write(f"  • {v} {k}")
        self.stdout.write(f"Kept: {User.objects.filter(is_staff=True).count()} staff account(s) and marketplace categories.")
        if not o["yes"]:
            if input("Type 'yes' to continue: ").strip().lower() != "yes":
                raise CommandError("Cancelled — nothing was deleted.")

        from apps.chat.models import Conversation
        from apps.market.models import BankTransferProof, OrderAgreement, PartnerRefund
        from apps.payments.models import BankReference, FraudAlert

        with transaction.atomic():
            Notification.objects.all().delete()
            WebhookEvent.objects.all().delete()
            AgencyLedgerEntry.objects.all().delete()
            Conversation.objects.all().delete()
            FraudAlert.objects.all().delete()
            for model in (BankTransferProof, OrderAgreement, PartnerRefund, BankReference):
                model.objects.all().delete()
            PaymentTransaction.objects.all().delete()
            # community finance
            GiveawayWinner.objects.all().delete()
            GiveawayEntry.objects.all().delete()
            GiveawayParticipant.objects.all().delete()
            Disbursement.objects.all().delete()
            Donation.objects.all().delete()
            # equb
            for model in (ScheduledPayment, JoinRequest, GroupEqubInvite, CallLog, Termination, EqubPayout, Contribution,
                          EqubPayment, EqubDraw, EqubRound, EqubMembership, GroupEqub):
                model.objects.all().delete()
            # marketplace
            for model in (OrderEvent, Installment, Order, Inquiry, SavedListing):
                model.objects.all().delete()
            FamilyMember.objects.all().delete()
            AgencyCustomer.objects.all().delete()
            if keep:
                ListingUnit.objects.exclude(status="BLOCKED").update(status="AVAILABLE", held_by=None, held_until=None,
                                                                     equb_group=None)
                Listing.objects.filter(status="SOLD_OUT").update(status="ACTIVE")
                Listing.objects.update(views=0)
                EqubGroup.objects.exclude(status="CANCELLED").update(status="OPEN", current_round=0)
                CrowdfundingCampaign.objects.update(raised_amount=0, fees_collected=0, disbursed_amount=0,
                                                    donations_count=0, donors_count=0)
                CrowdfundingCampaign.objects.filter(status="FUNDED").update(status="LIVE")
                Giveaway.objects.update(entries_issued=0, participants_count=0, referrals_count=0, drawn_at=None,
                                        public_input="")
                Giveaway.objects.filter(status__in=["CLOSED", "DRAWN"]).update(status="SCHEDULED")
            else:
                CrowdfundingCampaign.objects.all().delete()
                Giveaway.objects.all().delete()
                EqubGroup.objects.all().delete()
                EqubPackage.objects.all().delete()
                ListingImage.objects.all().delete()
                ListingUnit.objects.all().delete()
                Listing.objects.all().delete()
                AgencyStaff.objects.all().delete()
                from apps.agencies.models import PartnerBankAccount

                PartnerBankAccount.objects.all().delete()
                Agency.objects.all().delete()
            User.objects.filter(is_staff=True).update(referred_by=None)
            customers.delete()

        self.stdout.write(self.style.SUCCESS("Done — the system is clean. Sign in at /console/ to add your own data."))
