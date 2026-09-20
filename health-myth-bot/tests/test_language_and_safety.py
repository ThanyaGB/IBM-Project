"""Language layer and the emergency circuit breaker.

These two are the highest-consequence parts of the bot: a wrong language sends
the user a reply they cannot read, and a false emergency teaches them to ignore
the real one.  Both are tested with the exact inputs that used to fail.
"""

from __future__ import annotations

import pytest

from bot import language
from bot import safety_voice

DEV = range(0x0900, 0x0980)
KAN = range(0x0C80, 0x0D00)


def has_devanagari(text: str) -> bool:
    return any(ord(char) in DEV for char in text)


def has_kannada(text: str) -> bool:
    return any(ord(char) in KAN for char in text)


# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------
def test_supported_languages_are_en_hi_kn():
    assert language.SUPPORTED_LANGUAGES == ("en", "hi", "kn")


def test_swahili_is_gone_everywhere():
    """A leftover 'sw' key means one surface still ships the old language."""
    assert language.MENU_DIGITS == {"1": "en", "2": "hi", "3": "kn"}
    assert "sw" not in safety_voice.EMERGENCY_KEYWORDS
    assert "sw" not in safety_voice.EMERGENCY_MESSAGES

    # Every user-facing string table, found by shape: language-keyed dicts all
    # include 'en', which distinguishes them from _SCRIPT_RANGES (hi/kn only).
    tables = {
        name: value
        for name, value in vars(language).items()
        if isinstance(value, dict)
        and value
        and set(value).issubset({"en", "hi", "kn", "sw"})
        and "en" in value
    }
    assert len(tables) >= 12, "expected to find the language string tables"
    for name, table in tables.items():
        assert "sw" not in table, f"{name} still has a Swahili entry"
        assert set(table) == {"en", "hi", "kn"}, f"{name} is missing a language"


@pytest.mark.parametrize(
    "table_name",
    [
        "LANG_PROMPTS",
        "LANG_CONFIRMATIONS",
        "FEEDBACK_PROMPT",
        "FEEDBACK_ACK",
        "FLAG_PROMPT",
        "FLAG_ACK",
        "FLAG_DECLINED",
        "ERROR_REPLY",
        "AUDIO_FAILED_REPLY",
        "FALLBACK_MESSAGES",
        "BOT_PAUSED_REPLY",
        "RATE_LIMITED_REPLY",
        "LANGUAGE_NAMES",
        "LANGUAGE_PROMPT_NAMES",
    ],
)
def test_every_string_table_covers_every_language(table_name):
    table = getattr(language, table_name)
    for code in language.SUPPORTED_LANGUAGES:
        assert table.get(code), f"{table_name} has no {code} string"


@pytest.mark.parametrize(
    ("table_name", "code"),
    [
        ("FALLBACK_MESSAGES", "hi"),
        ("FALLBACK_MESSAGES", "kn"),
        ("ERROR_REPLY", "hi"),
        ("ERROR_REPLY", "kn"),
    ],
)
def test_translated_strings_are_not_just_english(table_name, code):
    text = getattr(language, table_name)[code]
    assert has_devanagari(text) or has_kannada(text), (
        f"{table_name}[{code}] is not in an Indic script"
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Native script decides outright.
        ("निमोनिया के लक्षण क्या हैं", "hi"),
        ("मुझे बुखार है", "hi"),
        ("ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ", "kn"),
        ("ಗರ್ಭಿಣಿಗೆ ಯಾವ ಆಹಾರ ಕೊಡಬೇಕು", "kn"),
        # Romanised — no native script to go on, which is the whole point of
        # the lexicon scorer.
        ("mujhe bukhar aur khaansi hai, kaun si dawa lein", "hi"),
        ("garbhavastha mein tetanus ka injection safe hai kya", "hi"),
        ("nange tumba jvara ide, enu madali", "kn"),
        ("nanna maguvige jvara ide", "kn"),
        ("usiru togoloke kasta aagide", "kn"),
        # English must not be captured by the romanised scorer.
        ("is the polio vaccine safe for my child", "en"),
        ("my child has a high fever, what should I do", "en"),
        ("can I drink water during pregnancy", "en"),
    ],
)
def test_detect_language(text, expected):
    assert language.detect_language(text) == expected


def test_short_ambiguous_input_falls_back_rather_than_guessing():
    """One Latin word carries no evidence; guessing would be worse."""
    assert language.detect_language("fever") == "en"
    assert language.detect_language("td") == "en"
    assert language.detect_language("") == "en"


def test_detect_language_never_returns_unsupported_code():
    for text in ("bonjour tout le monde", "hola amigo como estas", "hello there"):
        assert language.detect_language(text) in language.SUPPORTED_LANGUAGES


def test_normalise_language_coerces_unknown_to_english():
    assert language.normalise_language("sw") == "en"
    assert language.normalise_language(None) == "en"
    assert language.normalise_language("kn") == "kn"


# ---------------------------------------------------------------------------
# Emergency matching
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "chest pain",
        "heart attack",
        "severe bleeding",
        "difficulty breathing",
        "my child is unconscious",
        "he is having a seizure",
        "she swallowed poison",
        "stiff neck",
        "मुझे सीने में दर्द है",
        "मैं बेहोश हो गया",
        "मुझे दौरा पड़ रहा है",
        "सांस नहीं आ रही",
        "गर्दन में अकड़न है",
        "seene mein dard ho raha hai",
        # A qualifier between the body part and the symptom must not break the
        # match — this exact sentence reached production during smoke testing.
        "mere papa ko seene mein bahut dard ho raha hai",
        "mere pita ji ko seene me bahut tez dard hai",
        "chhati me dard hai",
        "gardan mein akadan hai",
        # ಎದೆಯಲ್ಲಿ is ಎದೆ + the locative suffix ಯಲ್ಲಿ, with no space between.
        "ನನ್ನ ಅಪ್ಪನ ಎದೆಯಲ್ಲಿ ತುಂಬಾ ನೋವು ಇದೆ",
        "ಎಚ್ಚರ ತಪ್ಪಿದೆ",
        "ಮಗುವಿಗೆ ಸೆಳೆತ ಬರುತ್ತಿದೆ",
        "ಕುತ್ತಿಗೆ ಬಿಗಿತ ಇದೆ",
        "ಉಸಿರು ಬರುತ್ತಿಲ್ಲ",
        "ede novu ide",
        "kuttige bigita",
        "prajne tappide",
    ],
)
def test_emergency_phrases_are_caught(text):
    is_emergency, message = safety_voice.check_emergency(text)
    assert is_emergency, f"{text!r} should be an emergency"
    assert message


@pytest.mark.parametrize(
    "text",
    [
        # Regressions: "fit" fired inside "benefit"/"fitness", so any question
        # about the benefits of exercise got the emergency message.
        "what is the benefit of walking",
        "benefits of exercise for fitness",
        "is fitness important for health",
        # Regressions: bare symptom words fired on mild complaints.
        "मुझे हल्का सिर दर्द है",
        "बुखार कितने दिन रहता है",
        "ಜ್ವರ ಇದ್ದರೆ ಏನು ಮಾಡಬೇಕು",
        "ನನ್ನ ಮಗು ಜ್ವರ ಇದೆ ಅಂತ ಹೇಳುತ್ತಿದೆ",
        # Fever is never an emergency however emphatically it is phrased: this
        # is the most common question the bot receives, and answering it with
        # "call an ambulance" hides the answer and devalues the real warning.
        "my child has a very high fever, what should I do",
        "मेरे बच्चे को बहुत तेज बुखार है, क्या करें",
        "ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ, ಏನು ಮಾಡಬೇಕು",
        "nange tumba jvara ide",
        "bahut tez bukhar hai",
        # Ordinary questions.
        "I feel fine today",
        "is ginger good for nausea",
        "can I drink coffee",
        "मैं ठीक हूँ",
        "दवाई लेनी है",
        "why does my online order say in transit",
        "is the polio vaccine safe for my child",
    ],
)
def test_non_emergencies_are_not_hijacked(text):
    is_emergency, _ = safety_voice.check_emergency(text)
    assert not is_emergency, f"{text!r} must not trigger the emergency reply"


def test_emergency_reply_is_in_the_users_language():
    """The one message that must be understood is never English-only."""
    for language_code, marker in (("hi", has_devanagari), ("kn", has_kannada)):
        hit, message = safety_voice.check_emergency("chest pain", language_code)
        assert hit
        assert marker(message), f"emergency reply for {language_code} is not translated"
        assert "EMERGENCY" in message  # stays recognisable across scripts


def test_emergency_matching_is_independent_of_stored_language():
    """Someone typing English while their session is set to Kannada.

    People code-switch under stress, so matching must not depend on the stored
    language — only the *reply* does.
    """
    hit, message = safety_voice.check_emergency("chest pain", "kn")
    assert hit and has_kannada(message)


def test_emergency_message_lookup_falls_back_to_english():
    assert safety_voice.get_emergency_message("fr") == safety_voice.get_emergency_message("en")


def test_check_emergency_returns_none_for_empty_input():
    assert safety_voice.check_emergency("") == (False, None)
    assert safety_voice.check_emergency("   ") == (False, None)
