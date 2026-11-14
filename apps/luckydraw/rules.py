"""
Official rules for a giveaway, shown in full inside the app (App Store guideline 5.3.2).

Apple requires that the rules are in the app (a link to a website is not enough), that the developer is the sponsor
(5.3.1) and that they say Apple is not a sponsor or involved in any way. The rules are built from the giveaway's own
settings, so they always match what the app does; staff can add extra rules per giveaway in the console.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

APPLE_STATEMENT = ("Apple Inc. is not a sponsor of this giveaway and is not involved in it in any way. "
                   "Apple is not responsible for the giveaway or its prizes.")


def _when(dt) -> str:
    return timezone.localtime(dt).strftime("%d %B %Y, %H:%M") if dt else "—"


def _money(v) -> str:
    return f"{v:,.2f} ETB"


def prize_provider(g) -> str:
    """The partner who provides the prize, if it is not Nebret itself. Nebret always remains the sponsor."""
    name = (g.sponsor_name or "").strip()
    if g.sponsor_type == g.SponsorType.CORPORATE and name and name.lower() != "nebret":
        return name
    return ""


def organizer_name() -> str:
    return f"Nebret ({settings.NEBRET['COMPANY_LEGAL_NAME']})"


def official_rules(g) -> list[dict]:
    """Ordered ``[{"title": …, "body": …}]`` sections."""
    rules = settings.NEBRET
    company = rules["COMPANY_LEGAL_NAME"]
    provider = prize_provider(g)
    n = g.referrals_per_entry
    cap = (f" Each person can earn at most {g.max_entries_per_user} entr{'y' if g.max_entries_per_user == 1 else 'ies'}."
           if g.max_entries_per_user else "")
    prize_kind = {
        "LISTING": "credit on a Nebret marketplace order for the prize item",
        "PURCHASE_CREDIT": "credit on any Nebret marketplace order",
        "EQUB_CONTRIBUTION": "paid contributions in a Nebret Equb",
    }.get(g.prize_type, "credit in Nebret")
    winners = g.number_of_winners

    sections = [
        ("Sponsor", f"This giveaway (\"{g.title}\") is organised and sponsored by {company}, the company that runs the "
                    f"Nebret app (\"Nebret\")."
                    + (f" The prize is provided by {provider} as Nebret's partner; Nebret remains the sole sponsor "
                       f"and is responsible for the giveaway." if provider else "")),
        ("Apple is not involved", APPLE_STATEMENT),
        ("No purchase necessary", "Entry is free. You never pay, donate or buy anything to enter or to win, and a "
                                  "purchase does not improve your chances. Donations to crowdfunding campaigns never "
                                  "earn entries."),
        ("Who can enter", "Anyone with a Nebret account in good standing who is 18 years or older. The winner may be "
                          "asked to show a valid ID before the prize is given. Accounts that are suspended or deleted "
                          "cannot take part."),
        ("Giveaway period", f"Entries can be earned from {_when(g.starts_at)} until {_when(g.ends_at)} "
                            f"(East Africa Time)."),
        ("How to enter", f"Share your Nebret referral code. For every {n} friend{'s' if n != 1 else ''} who create a "
                         f"new Nebret account with your code during the giveaway period, you receive one free entry "
                         f"automatically.{cap} Fake, duplicate or self-created accounts do not count, and Nebret may "
                         f"remove entries earned by fraud."),
        ("Prize", f"{winners} winner{'s' if winners != 1 else ''}. Each winner receives: {g.prize_title}, worth "
                  f"{_money(g.prize_value)}, given as {prize_kind}. The prize cannot be exchanged for cash or "
                  f"transferred to another person. Apple products are never offered as prizes."),
        ("How the winners are chosen", f"The draw takes place on {_when(g.draw_at)}. Winners are picked at random from "
                                       f"all entries with Nebret's provably fair draw: a secret code is fixed and its "
                                       f"fingerprint is published before the giveaway starts, so nobody – including "
                                       f"Nebret – can change the result. Anyone can check the draw afterwards with "
                                       f"\"Verify the draw\" in the app. Your chance of winning depends on how many "
                                       f"entries you have compared with the total number of entries."),
        ("Winners", "Winners are told in the app and listed (with a shortened name) on the giveaway page. A winner "
                    "uses the prize in the app under Community → My wins."),
        ("Changes and cancellation", "If the giveaway cannot run as planned for reasons outside Nebret's control, "
                                     "or if fraud affects it, Nebret may change, pause or cancel it and will explain "
                                     "why in the app."),
        ("Your data", "Nebret uses your account details only to run this giveaway and contact the winners, as "
                      "described in the Nebret privacy policy."),
        ("Questions", f"Contact {rules['SUPPORT_EMAIL']}. These rules follow the laws of Ethiopia."),
    ]
    out = [{"title": t, "body": b} for t, b in sections]
    extra = (g.rules or "").strip()
    if extra:
        out.insert(-1, {"title": "Additional rules for this giveaway", "body": extra})
    return out
