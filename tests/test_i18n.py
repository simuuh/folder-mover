"""Guards for translations/*.json — run this after editing a catalog."""

import re
from pathlib import Path

import pytest
from werkzeug.datastructures import LanguageAccept

import i18n

ROOT = Path(__file__).resolve().parent.parent
SOURCE = i18n.CATALOGS[i18n.SOURCE_LANGUAGE]
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Every way a key is referenced in code
KEY_PATTERNS = [
    re.compile(r"""\bt\(\s*['"]([a-z_]+\.[a-z0-9_.]+)['"]"""),              # t('…') in Jinja + JS
    re.compile(r"""\btStrong\(\s*['"]([a-z_]+\.[a-z0-9_.]+)['"]"""),        # tStrong('…') in JS
    re.compile(r"""\b(?:_|_err|api_error)\(\s*"([a-z_]+\.[a-z0-9_.]+)\""""),  # Python helpers
    re.compile(r"""(?:current_key|"key")\s*[=:]\s*"([a-z_]+\.[a-z0-9_.]+)\""""),
    re.compile(r"""return\s+"(api\.[a-z0-9_.]+)\""""),                      # _validate_move()
]
CODE_FILES = [*ROOT.glob("*.py"), *ROOT.glob("templates/*.html")]


def used_keys() -> set[str]:
    keys = set()
    for f in CODE_FILES:
        text = f.read_text(encoding="utf-8")
        for pattern in KEY_PATTERNS:
            keys.update(pattern.findall(text))
    return keys


def test_code_keys_exist_in_source_catalog():
    missing = sorted(k for k in used_keys() if k not in SOURCE)
    assert not missing, f"keys used in code but missing in de.json: {missing}"


def test_finds_a_plausible_number_of_keys():
    # protects the regexes above from silently matching nothing
    assert len(used_keys()) > 80


@pytest.mark.parametrize("lang", [lang for lang in i18n.LANGUAGES if lang != i18n.SOURCE_LANGUAGE])
def test_catalog_matches_source(lang):
    cat = i18n.CATALOGS[lang]
    assert set(cat) - set(SOURCE) == set(), "keys that do not exist in de.json"
    assert set(SOURCE) - set(cat) == set(), "keys missing (fallback to German would be used)"
    for key, text in cat.items():
        if text:
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(SOURCE[key])), \
                f"{lang}.json '{key}': placeholders differ from de.json"


def test_translate_placeholders_plural_and_fallback():
    assert i18n.translate("de", "scan.found", count=3) == "3 Einträge gefunden"
    assert i18n.translate("de", "scan.found", count=1) == "1 Eintrag gefunden"
    assert i18n.translate("xx", "scan.found", count=2) == "2 Einträge gefunden"   # unknown lang → de
    assert i18n.translate("de", "does.not.exist") == "does.not.exist"


def test_pick_language_order():
    browser_en = LanguageAccept([("en-US", 1), ("de", 0.5)])
    assert i18n.pick_language("en", "de", browser_en) == "en"       # cookie wins
    assert i18n.pick_language(None, "de", browser_en) == "de"       # then config
    assert i18n.pick_language(None, "auto", browser_en) == "en"     # then browser
    assert i18n.pick_language("fr", "auto", LanguageAccept([("fr", 1)])) == i18n.SOURCE_LANGUAGE
