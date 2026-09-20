"""Conservative text matching for automatic moderation.

There is no reliable way to recognise every translated slur without context.
Keep the default list narrow and let server owners add reviewed terms through
AUTOMOD_SLUR_TERMS (comma-separated) rather than banning ordinary words.
"""

import os
import re
import unicodedata


CONFUSABLES = str.maketrans({
    "а": "a", "ɑ": "a", "α": "a", "і": "i", "ı": "i", "ι": "i",
    "е": "e", "ε": "e", "ο": "o", "о": "o", "ɡ": "g", "ɢ": "g",
    "ｎ": "n", "ｉ": "i", "ｇ": "g", "ｅ": "e", "ａ": "a",
    "1": "i", "!": "i", "|": "i", "9": "g", "3": "e", "4": "a", "@": "a",
})
DEFAULT_SLURS = ("nigger", "nigga", "niggers", "niggas", "negre", "neger")
DISCORD_LINK_RE = re.compile(
    r"(?<![a-z0-9.-])(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com|discord\.gg)"
    r"(?:/[a-z0-9_/?#%&=.+~-]*)?(?![a-z0-9.-])", re.IGNORECASE,
)


def normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold()).translate(CONFUSABLES)
    return "".join(character for character in value if not unicodedata.combining(character)
                   and unicodedata.category(character) != "Cf")


def slur_pattern() -> re.Pattern[str]:
    configured = os.getenv("AUTOMOD_SLUR_TERMS", "")
    terms = set(DEFAULT_SLURS)
    terms.update(term.strip() for term in configured.split(",") if term.strip())
    patterns = []
    for term in terms:
        letters = re.sub(r"[^a-z]", "", normalise_text(term))
        if len(letters) >= 4:
            patterns.append(r"[^a-z0-9]{0,2}".join(map(re.escape, letters)))
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(sorted(patterns, key=len, reverse=True))
                      + r")(?![a-z0-9])")


SLUR_RE = slur_pattern()


def find_slur(value: str) -> bool:
    return SLUR_RE.search(normalise_text(value)) is not None


def contains_discord_link(value: str) -> bool:
    return DISCORD_LINK_RE.search(value) is not None


def redact_slurs(value: str) -> str:
    # Never echo a detected slur to the log channel. For obfuscated input,
    # redact the whole excerpt instead of trying to map normalised offsets.
    return "[message redacted: prohibited slur]" if find_slur(value) else value[:800]
