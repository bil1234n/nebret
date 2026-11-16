"""
Anti-bypass moderation: every chat message is screened on the server before anyone sees it.

Masked (replaced with ``•••``):

* phone numbers – Ethiopian (+251 9…, 09…, 07…, 011…) and international (+…), written with digits, spaced / dashed /
  dotted digits, Ethiopic numerals (፩ ፪ …) or digits spelled out in English or Amharic ("zero nine one…",
  "ዜሮ ዘጠኝ አንድ…");
* bank account numbers (any long run of digits) except the order's official account;
* e-mail addresses, also "name at gmail dot com" / "name[at]gmail.com";
* Telegram / WhatsApp / Viber / IMO handles and links, and social-media profile links.

Money amounts such as ``3,000,000`` are left alone. The original text is stored for Nebret review only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MASK = "•••"
NOTICE = "For your safety, contact details are hidden. Keep payments and messages in Nebret."

_EN_DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9"}
_AM_DIGITS = {"ዜሮ": "0", "ዝሮ": "0", "አንድ": "1", "ሁለት": "2", "ሶስት": "3", "ሦስት": "3", "አራት": "4", "አምስት": "5",
              "ስድስት": "6", "ሰባት": "7", "ስምንት": "8", "ዘጠኝ": "9"}
_ETHIOPIC = {"፩": "1", "፪": "2", "፫": "3", "፬": "4", "፭": "5", "፮": "6", "፯": "7", "፰": "8", "፱": "9"}
_DOUBLE = {"double": 2, "triple": 3, "ድርብ": 2}

_word_alt = "|".join(sorted(map(re.escape, [*_EN_DIGITS, *_AM_DIGITS, *_DOUBLE]), key=len, reverse=True))
_TOKEN = re.compile(rf"(?P<d>[0-9])|(?P<e>[፩-፱])|(?P<w>(?<![A-Za-zሀ-፿])(?:{_word_alt})(?![A-Za-zሀ-፿]))"
                    rf"|(?P<plus>\+)", re.IGNORECASE)
_SEP = re.compile(r"[\s\-\.\(\)/_,:*•]*")

EMAIL = re.compile(r"[\w.+\-]+\s*(?:@|\(at\)|\[at\]|\{at\}|\s+at\s+)\s*[\w\-]+\s*(?:\.|\(dot\)|\[dot\]|\s+dot\s+)\s*"
                   r"[a-z]{2,10}(?:\s*(?:\.|\s+dot\s+)\s*[a-z]{2,10})?", re.IGNORECASE)
LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|org|dog)|wa\.me|api\.whatsapp\.com|"
                  r"chat\.whatsapp\.com|whatsapp\.com|invite\.viber\.com|viber\.me|vb\.me|imo\.im|"
                  r"(?:m\.)?facebook\.com|fb\.(?:com|me)|instagram\.com|tiktok\.com|x\.com|twitter\.com|"
                  r"linkedin\.com|snapchat\.com)/?\S*|(?:viber|tg|whatsapp|tel|mailto|sms):\S+", re.IGNORECASE)
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
PLATFORM = re.compile(r"\b(?:telegram|whats\s?app|viber|imo|signal|wechat|ቴሌግራም|ዋትስአፕ|ቫይበር)\b"
                      r"(?:\s*(?:me|at|on|is|:|-|ላይ|በ))*\s*[@+]?[A-Za-z0-9_.\-]{3,}", re.IGNORECASE)
MONEY = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?$|^\d+\.\d{1,2}$")


@dataclass
class Result:
    text: str
    masked: bool = False
    kinds: list = field(default_factory=list)


def _digit_runs(text: str):
    """Spans of text that spell a number (digits, Ethiopic numerals, digit words, separators in between)."""
    tokens = list(_TOKEN.finditer(text))
    i = 0
    while i < len(tokens):
        start = tokens[i].start()
        digits, end, j, repeat = "", tokens[i].end(), i, 1
        while j < len(tokens):
            t = tokens[j]
            if j > i and not _SEP.fullmatch(text[end:t.start()]):
                break
            if t.group("d"):
                digits += t.group("d") * repeat
                repeat = 1
            elif t.group("e"):
                digits += _ETHIOPIC[t.group("e")] * repeat
                repeat = 1
            elif t.group("w"):
                w = t.group("w").lower() if t.group("w").isascii() else t.group("w")
                if w in _DOUBLE:
                    repeat = _DOUBLE[w]
                else:
                    digits += (_EN_DIGITS.get(w) or _AM_DIGITS.get(w, "")) * repeat
                    repeat = 1
            elif t.group("plus") and j == i:
                digits += "+"
            elif t.group("plus"):
                break
            end = t.end()
            j += 1
        yield start, end, digits
        i = max(j, i + 1)


def _is_contact_number(raw: str, digits: str, allowed: set[str]) -> str | None:
    only = digits.lstrip("+")
    if not only:
        return None
    if MONEY.match(raw.strip()):
        return None
    if only in allowed:
        return None
    if re.fullmatch(r"(?:251|0)?[79]\d{8}", only) or re.fullmatch(r"(?:251|0)1\d{8}", only):
        return "phone"
    if digits.startswith("+") and 8 <= len(only) <= 15:
        return "phone"
    if len(only) >= 9:
        return "account_or_phone"
    return None


def screen(text: str, *, allowed_numbers: list[str] | None = None) -> Result:
    """Mask contact details in ``text``. ``allowed_numbers`` – e.g. the order's official account number."""
    allowed = {re.sub(r"\D", "", n) for n in (allowed_numbers or []) if n}
    out, kinds = text or "", []

    def sub(pattern, kind, s):
        def repl(m):
            kinds.append(kind)
            return MASK
        return pattern.sub(repl, s)

    out = sub(LINK, "link", out)
    out = sub(EMAIL, "email", out)
    pieces, last = [], 0
    for start, end, digits in _digit_runs(out):
        kind = _is_contact_number(out[start:end], digits, allowed)
        if kind:
            pieces.append(out[last:start])
            pieces.append(MASK)
            last = end
            kinds.append(kind)
    pieces.append(out[last:])
    out = "".join(pieces)
    out = sub(PLATFORM, "messaging_app", out)
    out = sub(HANDLE, "handle", out)
    return Result(text=out, masked=bool(kinds), kinds=kinds)
