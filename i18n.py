"""
Minimal i18n: flat JSON catalogs in translations/<lang>.json.

German (de) is the source language — every key must exist there. Other
catalogs may be incomplete; missing or empty entries fall back to German.

Placeholders use {name}. If a `count` argument equals 1 and a `<key>_one`
entry exists, that singular form is used.
"""

import json
import re
from functools import cache
from pathlib import Path

TRANSLATIONS_DIR = Path(__file__).parent / "translations"
SOURCE_LANGUAGE = "de"
COOKIE = "fm-lang"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _load() -> dict[str, dict[str, str]]:
    return {
        f.stem: json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(TRANSLATIONS_DIR.glob("*.json"))
    }


CATALOGS = _load()
LANGUAGES = sorted(CATALOGS)


@cache
def catalog(lang: str) -> dict[str, str]:
    """Catalog for `lang`, completed with the source language."""
    own = {k: v for k, v in CATALOGS.get(lang, {}).items() if v}
    return {**CATALOGS[SOURCE_LANGUAGE], **own}


def translate(lang: str, key: str, **args) -> str:
    cat = catalog(lang)
    if args.get("count") == 1 and f"{key}_one" in cat:
        key = f"{key}_one"
    text = cat.get(key, key)
    return _PLACEHOLDER.sub(lambda m: str(args[m.group(1)]) if m.group(1) in args else m.group(0), text)


def pick_language(cookie: str | None, configured: str | None, accept_languages=None) -> str:
    """Cookie (header switch) → config `language` → browser language → source language."""
    if cookie in CATALOGS:
        return cookie
    if configured in CATALOGS:
        return configured
    # Browser languages in priority order, compared by primary tag ("en-US" → "en").
    # (werkzeug's best_match() would prefer an exact low-priority "de" over "en-US".)
    for value, _quality in accept_languages or []:
        primary = value.split("-")[0].lower()
        if primary in CATALOGS:
            return primary
    return SOURCE_LANGUAGE
