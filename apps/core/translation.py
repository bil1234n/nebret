"""
Show content people type (product titles, descriptions, features, categories, company profiles, Equb names and
terms) in the app's language: Amharic, Afaan Oromoo, Arabic or English.

* Provider: Google Cloud Translation (``GOOGLE_TRANSLATE_API_KEY``) – supports am / om / ar – or Claude
  (``ANTHROPIC_API_KEY``). Without a key, content is shown as typed.
* Every translation is cached in :class:`~apps.core.models.Translation` by a hash of the source text, so each text is
  translated once; editing it simply creates a new translation.
* :func:`localize` walks an API response, translates the listed fields in one batch and puts the results back.
  Products are also translated in the background when they are saved, so most views never wait.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

import requests
from django.conf import settings
from django.db import IntegrityError

log = logging.getLogger(__name__)

LANGS = ("am", "om", "ar", "en")
LANG_NAMES = {"am": "Amharic", "om": "Afaan Oromoo (Oromo, Latin script)", "ar": "Arabic", "en": "English"}
MAX_BATCH = 120
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def request_lang(request) -> str:
    """``?lang=`` or the app's ``Accept-Language`` header (``am``, ``om``, ``ar``, ``en``)."""
    if request is None:
        return "en"
    raw = (getattr(request, "query_params", None) or request.GET).get("lang") or \
        request.headers.get("Accept-Language", "")
    code = raw.split(",")[0].split(";")[0].split("-")[0].strip().lower()
    return code if code in LANGS else "en"


def provider() -> str:
    if getattr(settings, "GOOGLE_TRANSLATE_API_KEY", ""):
        return "google"
    if getattr(settings, "ANTHROPIC_API_KEY", ""):
        return "claude"
    return ""


def enabled() -> bool:
    return bool(provider())


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def translatable(text) -> bool:
    return isinstance(text, str) and bool(text.strip()) and bool(_LETTERS.search(text)) and len(text) <= 8000


# --------------------------------------------------------------------------- providers
def _google(texts: list[str], lang: str) -> list[str | None]:
    resp = requests.post("https://translation.googleapis.com/language/translate/v2",
                         params={"key": settings.GOOGLE_TRANSLATE_API_KEY},
                         json={"q": texts, "target": lang, "format": "text"}, timeout=20)
    resp.raise_for_status()
    out = []
    for src, t in zip(texts, resp.json()["data"]["translations"]):
        # already in the target language → keep what the seller typed
        out.append(src if t.get("detectedSourceLanguage", "")[:2] == lang else t.get("translatedText") or src)
    return out


def _claude(texts: list[str], lang: str) -> list[str | None]:
    prompt = (
        f"Translate each string in this JSON array into {LANG_NAMES[lang]} for a marketplace and savings app in "
        "Ethiopia (cars, homes, e-bikes, Equb savings groups). Keep brand names, model names, numbers, units and "
        "currency codes (ETB) unchanged. If a string is already in the target language, return it unchanged. "
        "Reply with ONLY a JSON array of the translated strings, same length and order.\n\n"
        + json.dumps(texts, ensure_ascii=False))
    resp = requests.post("https://api.anthropic.com/v1/messages", timeout=40, headers={
        "x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": getattr(settings, "TRANSLATION_MODEL", "claude-haiku-5-5"), "max_tokens": 8000,
              "messages": [{"role": "user", "content": prompt}]})
    resp.raise_for_status()
    body = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
    start, end = body.find("["), body.rfind("]")
    data = json.loads(body[start:end + 1])
    if not isinstance(data, list) or len(data) != len(texts):
        raise ValueError("translation count mismatch")
    return [str(x) if x else None for x in data]


# --------------------------------------------------------------------------- cache
def cached(texts, lang: str) -> dict[str, str]:
    from .models import Translation

    hashes = {_h(t): t for t in texts}
    rows = Translation.objects.filter(lang=lang, source_hash__in=list(hashes)).values_list("source_hash", "text")
    return {hashes[h]: text for h, text in rows}


def translate_many(texts, lang: str, *, fetch: bool = True) -> dict[str, str]:
    """``{source: translation}`` for every text we have (cached, plus newly fetched when ``fetch``)."""
    from .models import Translation

    texts = list(dict.fromkeys(t for t in texts if translatable(t)))
    if lang not in LANGS or not texts:
        return {}
    have = cached(texts, lang)
    missing = [t for t in texts if t not in have]
    kind = provider()
    if missing and fetch and kind:
        for i in range(0, len(missing), MAX_BATCH):
            chunk = missing[i:i + MAX_BATCH]
            try:
                result = _google(chunk, lang) if kind == "google" else _claude(chunk, lang)
            except Exception as exc:  # noqa: BLE001 – never break a page because translation failed
                log.warning("Translation to %s failed: %s", lang, exc)
                break
            for src, text in zip(chunk, result):
                if not text:
                    continue
                have[src] = text
                try:
                    Translation.objects.create(source_hash=_h(src), lang=lang, source=src, text=text, provider=kind)
                except IntegrityError:
                    pass
    return have


# --------------------------------------------------------------------------- applying to API data
def _walk(node, path: list[str], visit):
    """Call ``visit(container, key)`` for every string at ``path`` (``*`` = every item of a list)."""
    if not path or node is None:
        return
    head, rest = path[0], path[1:]
    if head == "*":
        if isinstance(node, list):
            for i, item in enumerate(node):
                if rest:
                    _walk(item, rest, visit)
                elif isinstance(item, str):
                    visit(node, i)
        return
    if isinstance(node, dict) and head in node:
        if rest:
            _walk(node[head], rest, visit)
        elif isinstance(node[head], str):
            visit(node, head)
        elif isinstance(node[head], list):
            for i, item in enumerate(node[head]):
                if isinstance(item, str):
                    visit(node[head], i)


def localize(data, lang: str, paths: list[str]):
    """Translate the strings at ``paths`` (dotted, ``*`` for lists) in ``data`` in place. Returns ``data``."""
    if lang not in ("am", "om", "ar", "en") or not enabled() or data is None:
        return data
    found: list[tuple[object, object]] = []
    for p in paths:
        _walk(data, p.split("."), lambda c, k: found.append((c, k)))
    texts = [c[k] for c, k in found if translatable(c[k])]
    if not texts:
        return data
    table = translate_many(texts, lang)
    for c, k in found:
        v = c[k]
        if v in table:
            c[k] = table[v]
    return data


# What to translate in each API response.
LISTING_FIELDS = ["title", "summary", "description", "features", "category.name", "condition_label",
                  "specs.*.label", "specs.*.value", "key_specs.*.label", "key_specs.*.value", "company.description",
                  "company.kind_label", "units.*.label", "equb_groups.*.name", "equb_groups.*.every",
                  "equb_groups.*.frequency_label"]
LISTING_PAGE = ["results.*." + f for f in LISTING_FIELDS] + ["*." + f for f in LISTING_FIELDS]
CATEGORY_FIELDS = ["*.name", "*.description", "*.group_label", "results.*.name", "results.*.description",
                   "*.spec_fields.*.label"]
COMPANY_FIELDS = ["description", "kind_label", "listings.*.title", "listings.*.summary"]
ORDER_FIELDS = ["listing_title", "listing.title", "results.*.listing_title", "results.*.listing.title",
                "*.listing_title", "*.listing.title"]
EQUB_FIELDS = ["name", "description", "terms_content", "terms", "duration_label", "equb_package.name",
               "product.title", "results.*.name", "results.*.description", "results.*.equb_package.name",
               "results.*.product.title", "results.*.duration_label", "*.name", "*.description", "*.terms_content",
               "*.equb_package.name", "*.product.title", "*.duration_label"]


def listing_texts(listing) -> list[str]:
    from apps.market.serializers import _spec_rows

    out = [listing.title, listing.summary, listing.description, listing.category.name, *(listing.features or [])]
    for r in _spec_rows(listing):
        out += [r["label"], r["value"] if isinstance(r["value"], str) else ""]
    return [t for t in out if translatable(t)]


def warm_listing(listing, langs=("am", "om", "ar", "en")) -> int:
    """Translate a product into every app language now (called after a product is saved)."""
    if not enabled():
        return 0
    texts = listing_texts(listing)
    return sum(len(translate_many(texts, lang)) for lang in langs)


def warm_missing(limit: int = 25) -> int:
    """Background catch-up (scheduler): translate products that are not fully translated yet."""
    from apps.market.models import Listing

    if not enabled():
        return 0
    n = 0
    for listing in Listing.objects.filter(status="ACTIVE").select_related("category").order_by("-updated_at")[:200]:
        texts = listing_texts(listing)
        if all(len(cached(texts, lang)) >= len(set(texts)) for lang in ("am", "om", "ar")):
            continue
        warm_listing(listing, ("am", "om", "ar"))
        n += 1
        if n >= limit:
            break
    return n


class LocalizedResponseMixin:
    """DRF views: translate ``localize_fields`` of successful GET responses into the app's language."""

    localize_fields: list[str] = []

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if request.method == "GET" and response.status_code == 200 and self.localize_fields and enabled():
            lang = request_lang(request)
            try:
                localize(response.data, lang, self.localize_fields)
                response["Content-Language"] = lang
            except Exception:  # noqa: BLE001
                log.exception("localize failed")
        return response
