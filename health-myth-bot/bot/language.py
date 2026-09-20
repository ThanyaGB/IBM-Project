"""Language layer for health-myth-bot.

Everything language-specific lives here, so adding a fourth language means
editing this file plus the corpus — not chasing ~30 scattered string tables.

What lives here
---------------
* SUPPORTED_LANGUAGES and the digit → language menu.
* Every user-facing string (menu, confirmations, prompts, errors, fallbacks).
* detect_language(), which understands native script *and* romanised input
  ("mujhe bukhar hai", "nange jvara ide"), not just langdetect on native text.

Detection order
---------------
1. Script ranges decide outright.  Devanagari and Kannada codepoints are
   unambiguous, and langdetect is both unnecessary and unreliable on short
   strings like "बुखार".
2. Latin script is scored against romanised Hinglish / Kanglish lexicons.  A
   confident win switches language; otherwise the message stays English —
   guessing Hindi for an English question would send the user a reply they
   cannot read, which is worse than not detecting at all.
3. langdetect is consulted last, only for text long enough to be reliable.

Review provenance
-----------------
Strings here are UI copy and were written by hand.  They are safe to ship
without clinical review.  That is *not* true of translated verified_fact
text in health_facts.json — see the "review" block on each corpus record.
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------
# Kannada replaces Swahili: the deployed pilot is Karnataka, and Swahili never
# matched the intended user base.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "hi", "kn")
DEFAULT_LANGUAGE = "en"

# Languages written in Latin script, where romanised input is expected.
LATIN_SCRIPT_LANGUAGES = frozenset({"en"})

# Menu digits → language code.
MENU_DIGITS: dict[str, str] = {"1": "en", "2": "hi", "3": "kn"}

# English name and endonym.  The endonym is what users recognise; the English
# name is what the LLM needs to be told to reply in.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi (हिन्दी)",
    "kn": "Kannada (ಕನ್ನಡ)",
}

# Name for the LLM prompt ("Respond ONLY in <this>").
LANGUAGE_PROMPT_NAMES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi (Devanagari script)",
    "kn": "Kannada (Kannada script)",
}

SCRIPT_NAMES: dict[str, str] = {
    "en": "Latin",
    "hi": "Devanagari",
    "kn": "Kannada script",
}

LANGUAGE_MENU = "\n".join(
    [
        "1 – English",
        "2 – हिन्दी (Hindi)",
        "3 – ಕನ್ನಡ (Kannada)",
    ]
)

# ---------------------------------------------------------------------------
# Script ranges
# ---------------------------------------------------------------------------
_SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "hi": (0x0900, 0x097F),  # Devanagari
    "kn": (0x0C80, 0x0CFF),  # Kannada
}

_DIGIT_RE = re.compile(r"\d")


def script_counts(text: str) -> dict[str, int]:
    """Count how many characters of `text` belong to each Indic script."""
    counts = {code: 0 for code in _SCRIPT_RANGES}
    for char in text or "":
        point = ord(char)
        for code, (low, high) in _SCRIPT_RANGES.items():
            if low <= point <= high:
                counts[code] += 1
                break
    return counts


# ---------------------------------------------------------------------------
# User-facing strings
# ---------------------------------------------------------------------------
LANG_PROMPTS: dict[str, str] = {
    "en": (
        "Please reply with a number to choose your language:\n"
        f"{LANGUAGE_MENU}"
    ),
    "hi": (
        "कृपया अपनी भाषा चुनने के लिए एक नंबर भेजें:\n"
        f"{LANGUAGE_MENU}"
    ),
    "kn": (
        "ನಿಮ್ಮ ಭಾಷೆಯನ್ನು ಆಯ್ಕೆ ಮಾಡಲು ದಯವಿಟ್ಟು ಒಂದು ಸಂಖ್ಯೆಯನ್ನು ಕಳುಹಿಸಿ:\n"
        f"{LANGUAGE_MENU}"
    ),
}

LANG_CONFIRMATIONS: dict[str, str] = {
    "en": "✅ Language set to *English*. Ask me any health question!",
    "hi": "✅ भाषा *हिन्दी* पर सेट की गई। मुझसे कोई भी स्वास्थ्य प्रश्न पूछें!",
    "kn": "✅ ಭಾಷೆಯನ್ನು *ಕನ್ನಡ* ಗೆ ಹೊಂದಿಸಲಾಗಿದೆ. ನನ್ನನ್ನು ಯಾವುದೇ ಆರೋಗ್ಯ ಪ್ರಶ್ನೆ ಕೇಳಿ!",
}

FEEDBACK_PROMPT: dict[str, str] = {
    "en": "💬 Was this helpful? Reply 👍 or 👎",
    "hi": "💬 क्या यह उपयोगी था? 👍 या 👎 जवाब दें",
    "kn": "💬 ಇದು ಸಹಾಯಕವಾಗಿತ್ತೇ? 👍 ಅಥವಾ 👎 ಎಂದು ಉತ್ತರಿಸಿ",
}

FEEDBACK_ACK: dict[str, str] = {
    "en": "Thank you for your feedback! 🙏",
    "hi": "आपकी प्रतिक्रिया के लिए धन्यवाद! 🙏",
    "kn": "ನಿಮ್ಮ ಪ್ರತಿಕ್ರಿಯೆಗೆ ಧನ್ಯವಾದಗಳು! 🙏",
}

FLAG_PROMPT: dict[str, str] = {
    "en": (
        "This topic isn't in our verified database yet. Would you like to "
        "flag it for review by health officials? Reply *YES* to report."
    ),
    "hi": (
        "यह विषय अभी हमारे सत्यापित डेटाबेस में नहीं है। क्या आप इसे स्वास्थ्य "
        "अधिकारियों की समीक्षा के लिए फ्लैग करना चाहेंगे? रिपोर्ट करने के लिए "
        "*YES* जवाब दें।"
    ),
    "kn": (
        "ಈ ವಿಷಯವು ಇನ್ನೂ ನಮ್ಮ ಪರಿಶೀಲಿಸಿದ ಮಾಹಿತಿಗಳಲ್ಲಿ ಇಲ್ಲ. ಆರೋಗ್ಯ ಅಧಿಕಾರಿಗಳ "
        "ಪರಿಶೀಲನೆಗೆ ಇದನ್ನು ಗುರುತಿಸಲು ಬಯಸುವಿರಾ? ವರದಿ ಮಾಡಲು *YES* ಎಂದು ಉತ್ತರಿಸಿ."
    ),
}

FLAG_ACK: dict[str, str] = {
    "en": (
        "✅ Reported! Our health team will review it. Thank you for helping "
        "improve our database. 🙏"
    ),
    "hi": (
        "✅ रिपोर्ट हो गई! हमारी स्वास्थ्य टीम इसकी समीक्षा करेगी। हमारे डेटाबेस "
        "को बेहतर बनाने में मदद के लिए धन्यवाद। 🙏"
    ),
    "kn": (
        "✅ ವರದಿ ಮಾಡಲಾಗಿದೆ! ನಮ್ಮ ಆರೋಗ್ಯ ತಂಡ ಇದನ್ನು ಪರಿಶೀಲಿಸುತ್ತದೆ. ನಮ್ಮ "
        "ಮಾಹಿತಿಗಳನ್ನು ಸುಧಾರಿಸಲು ಸಹಾಯ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. 🙏"
    ),
}

FLAG_DECLINED: dict[str, str] = {
    "en": "Understood. Feel free to ask another question any time! 😊",
    "hi": "ठीक है। जब चाहें कोई और प्रश्न पूछ सकते हैं! 😊",
    "kn": "ಅರ್ಥವಾಯಿತು. ಯಾವಾಗ ಬೇಕಾದರೂ ಇನ್ನೊಂದು ಪ್ರಶ್ನೆ ಕೇಳಿ! 😊",
}

ERROR_REPLY: dict[str, str] = {
    "en": (
        "⚠️ Something went wrong on our end. Please try again in a moment. "
        "If this persists, contact your local health centre."
    ),
    "hi": (
        "⚠️ हमारी ओर से कुछ गड़बड़ हुई है। कृपया थोड़ी देर बाद पुनः प्रयास करें। "
        "यह जारी रहे तो अपने नजदीकी स्वास्थ्य केंद्र से संपर्क करें।"
    ),
    "kn": (
        "⚠️ ನಮ್ಮ ಕಡೆಯಿಂದ ಏನೋ ತಪ್ಪಾಗಿದೆ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ "
        "ಪ್ರಯತ್ನಿಸಿ. ಇದು ಮುಂದುವರಿದರೆ, ನಿಮ್ಮ ಸ್ಥಳೀಯ ಆರೋಗ್ಯ ಕೇಂದ್ರವನ್ನು "
        "ಸಂಪರ್ಕಿಸಿ."
    ),
}

AUDIO_FAILED_REPLY: dict[str, str] = {
    "en": (
        "I wasn't able to process your voice note. Could you please type your "
        "question instead? 🙏"
    ),
    "hi": (
        "मैं आपका वॉइस नोट समझ नहीं पाया। कृपया अपना प्रश्न टाइप करके भेजें। 🙏"
    ),
    "kn": (
        "ನಿಮ್ಮ ಧ್ವನಿ ಸಂದೇಶವನ್ನು ಪ್ರಕ್ರಿಯೆ ಮಾಡಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ನಿಮ್ಮ "
        "ಪ್ರಶ್ನೆಯನ್ನು ಟೈಪ್ ಮಾಡಿ ಕಳುಹಿಸಿ. 🙏"
    ),
}

AUDIO_TOO_LONG_REPLY: dict[str, str] = {
    "en": (
        "That voice note was too long for me to process. Could you ask your "
        "question in a shorter recording, or type it? 🙏"
    ),
    "hi": (
        "यह वॉइस नोट प्रोसेस करने के लिए बहुत लंबा था। कृपया छोटी रिकॉर्डिंग में "
        "अपना प्रश्न पूछें या टाइप करें। 🙏"
    ),
    "kn": (
        "ಆ ಧ್ವನಿ ಸಂದೇಶವು ಪ್ರಕ್ರಿಯೆ ಮಾಡಲು ತುಂಬಾ ಉದ್ದವಾಗಿತ್ತು. ದಯವಿಟ್ಟು ಚಿಕ್ಕ "
        "ಧ್ವನಿ ಸಂದೇಶದಲ್ಲಿ ಅಥವಾ ಟೈಪ್ ಮಾಡಿ ಪ್ರಶ್ನೆ ಕೇಳಿ. 🙏"
    ),
}

FALLBACK_MESSAGES: dict[str, str] = {
    "en": (
        "I'm sorry, I couldn't find a verified answer for that topic in my "
        "database yet. For reliable health information, please consult your "
        "local health worker or visit the nearest clinic. 🏥"
    ),
    "hi": (
        "मुझे खेद है, मेरे डेटाबेस में अभी इस विषय का सत्यापित उत्तर नहीं मिला। "
        "विश्वसनीय स्वास्थ्य जानकारी के लिए, कृपया अपने स्थानीय स्वास्थ्य "
        "कार्यकर्ता से या नजदीकी क्लिनिक जाकर सलाह लें। 🏥"
    ),
    "kn": (
        "ಕ್ಷಮಿಸಿ, ಈ ವಿಷಯದ ಬಗ್ಗೆ ಪರಿಶೀಲಿಸಿದ ಉತ್ತರ ನನ್ನ ಮಾಹಿತಿಗಳಲ್ಲಿ ಇನ್ನೂ "
        "ಸಿಗಲಿಲ್ಲ. ವಿಶ್ವಾಸಾರ್ಹ ಆರೋಗ್ಯ ಮಾಹಿತಿಗಾಗಿ, ದಯವಿಟ್ಟು ನಿಮ್ಮ ಸ್ಥಳೀಯ ಆರೋಗ್ಯ "
        "ಕಾರ್ಯಕರ್ತರನ್ನು ಸಂಪರ್ಕಿಸಿ ಅಥವಾ ಹತ್ತಿರದ ಆರೋಗ್ಯ ಕೇಂದ್ರಕ್ಕೆ ಭೇಟಿ ನೀಡಿ. 🏥"
    ),
}

# The bot is paused (kill switch).  Shown once per inbound message while off.
BOT_PAUSED_REPLY: dict[str, str] = {
    "en": (
        "🛠️ The service is briefly paused for maintenance. Please ask again "
        "later, or contact your local health worker if your question is urgent."
    ),
    "hi": (
        "🛠️ सेवा रखरखाव के लिए कुछ समय के लिए रोकी गई है। कृपया बाद में फिर "
        "पूछें, और अगर आपका प्रश्न ज़रूरी है तो अपने स्वास्थ्य कार्यकर्ता से "
        "संपर्क करें।"
    ),
    "kn": (
        "🛠️ ಸೇವೆಯನ್ನು ಸ್ವಲ್ಪ ಸಮಯ ನಿರ್ವಹಣೆಗಾಗಿ ನಿಲ್ಲಿಸಲಾಗಿದೆ. ದಯವಿಟ್ಟು ನಂತರ ಮತ್ತೆ "
        "ಕೇಳಿ, ಅಥವಾ ನಿಮ್ಮ ಪ್ರಶ್ನೆ ತುರ್ತಾದರೆ ಆರೋಗ್ಯ ಕಾರ್ಯಕರ್ತರನ್ನು ಸಂಪರ್ಕಿಸಿ."
    ),
}

# Rate-limited: a user is sending far more messages per hour than a person
# normally would (or a script is).
RATE_LIMITED_REPLY: dict[str, str] = {
    "en": (
        "⏳ You've sent a lot of questions just now. Please wait a little while "
        "and try again."
    ),
    "hi": "⏳ आपने अभी बहुत सारे प्रश्न भेजे हैं। कृपया थोड़ी देर बाद पुनः प्रयास करें।",
    "kn": "⏳ ನೀವು ಇದೀಗ ಬಹಳಷ್ಟು ಪ್ರಶ್ನೆಗಳನ್ನು ಕಳುಹಿಸಿದ್ದೀರಿ. ದಯವಿಟ್ಟು ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
}

# Outbound advisories may only be sent inside the 24-hour customer service
# window the user's own message opened — outside it, Meta bills per template
# message.  This is the internal note when a campaign is skipped for that
# reason; it is never sent to users.
WINDOW_CLOSED_NOTE = "outside 24h service window — send skipped to stay free"

# Marker phrases used to recognise our own fallback text.  Kept here so
# app.py never has to hardcode translated substrings.
FALLBACK_MARKERS: tuple[str, ...] = (
    "couldn't find a verified answer",
    "सत्यापित उत्तर नहीं मिला",
    "ಪರಿಶೀಲಿಸಿದ ಉತ್ತರ",
)

# ---------------------------------------------------------------------------
# Detection triggers
# ---------------------------------------------------------------------------
# Words a user can send to reopen the language menu.
LANG_KEYWORDS: frozenset[str] = frozenset(
    {
        "language",
        "languages",
        "change language",
        "bhasha",
        "bhasha badlo",
        "bhasha badle",
        "भाषा",
        "भाषा बदलो",
        "ಭಾಷೆ",
        "ಭಾಷೆ ಬದಲಾಯಿಸಿ",
    }
)

# Yes-words across languages and scripts.  Any of these confirms a flag.
YES_WORDS: frozenset[str] = frozenset(
    {
        "yes",
        "y",
        "yeah",
        "yep",
        "ok",
        "okay",
        "haan",
        "han",
        "ha",
        "haa",
        "हाँ",
        "हां",
        "हा",
        "ಜಿ",
        "ಹೌದು",
        "ಹೂಂ",
        "haudu",
        "howdu",
        "houdu",
    }
)

NO_WORDS: frozenset[str] = frozenset(
    {
        "no",
        "n",
        "nope",
        "nahi",
        "nahin",
        "na",
        "नहीं",
        "नही",
        "ಇಲ್ಲ",
        "ಇಲ್ಲ",
        "illa",
        "alla",
    }
)

FEEDBACK_MAP: dict[str, int] = {
    "👍": 1,
    "👎": -1,
    "+1": 1,
    "-1": -1,
    "1": 1,
    "thumbs up": 1,
    "thumbs down": -1,
    "good": 1,
    "bad": -1,
    "helpful": 1,
    "useful": 1,
    "upvote": 1,
    "downvote": -1,
    "sahi": 1,
    "सही": 1,
    "अच्छा": 1,
    "સા": 1,
    "ಸರಿ": 1,
    "ಒಳ್ಳೆಯದು": 1,
    "ಚೆನ್ನಾಗಿದೆ": 1,
    "sari": 1,
    "chennagide": 1,
}

# ---------------------------------------------------------------------------
# Romanised (Latin-script) lexicons
# ---------------------------------------------------------------------------
# Hindi/Kannada typed in Latin script never reaches langdetect.  These markers
# are common *function* words plus a few high-signal content words; a message
# only switches language on a confident margin, so an English question that
# happens to share a token ("do", "no") still stays English.
_ROMANISED_MARKERS: dict[str, frozenset[str]] = {
    "hi": frozenset(
        {
            "aap", "acha", "apna", "apni", "aur", "bachcha", "bachche",
            "bacha", "batao", "bataye", "bhai", "bukhar", "bukhaar", "chahiye",
            "dard", "dawa", "dawai", "dhoka", "hai", "hain", "hoga", "hogi",
            "hota", "hoti", "injection", "kab", "kaise", "karta", "karti",
            "ke", "ki", "koi", "kuch", "kya", "kyun", "kyu", "lekin", "liye",
            "mein", "mera", "meri", "mujhe", "nahi", "nahin", "pani", "paani",
            "pregnan", "raha", "rahi", "saans", "sans", "se", "sirf", "teeka",
            "tez", "tika", "tumhara", "wala", "wali", "yeh", "ye", "zukaam",
            "zukam", "sardi", "khansi", "ilaj", "ilaaj", "aspatal", "haspatal",
            "doctor", "daktar", "sehat", "swasthya",
        }
    ),
    "kn": frozenset(
        {
            "aagide", "aagutta", "alla", "amba", "avana", "avara", "avanu",
            "avalu", "baagide", "bahala", "barutte", "beku", "bekagide",
            "chennagide", "davai", "dawa", "ede", "edey", "emba", "endu",
            "enu", "hege", "beki", "hogi", "hosa", "howdu", "haudu", "ide",
            "idu", "idhu", "illa", "illad", "iskole", "jvara", "jwara",
            "kannada", "kelavu", "kelsa", "kuda", "maadi", "maadu", "madu",
            "matte", "murche", "namma", "nanage", "nanna", "nanu", "nimma",
            "novu", "ondu", "dakhtara", "aspatal", "usiru", "yaake", "yake",
            "yavaga", "yavudu", "tumba", "thumba", "visha", "jvara", "jala",
            "oota", "ootava", "haalu", "nidde", "sigutte", "siguvudilla",
            "bekilla", "gottilla", "kottre", "tilsi",
        }
    ),
}

# A single shared marker ("no", "do") must never be enough.  Two markers plus
# a one-token margin is the smallest signal that survived experimentation on
# realistic questions like "my child has fever, is that ok".
_MIN_MARKER_SCORE = 2
_MIN_MARKER_MARGIN = 1

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def get_string(table: dict[str, str], language: str) -> str:
    """Look up `language` in a string table, falling back to English."""
    return table.get(language) or table[DEFAULT_LANGUAGE]


def is_supported(language: str | None) -> bool:
    return bool(language) and language in SUPPORTED_LANGUAGES


def normalise_language(language: str | None) -> str:
    """Coerce anything to a supported language code."""
    if is_supported(language):
        return language  # type: ignore[return-value]
    return DEFAULT_LANGUAGE


def is_fallback_response(text: str) -> bool:
    """True when `text` is one of our own "no verified answer" messages."""
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in FALLBACK_MARKERS)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def score_romanised(text: str) -> dict[str, int]:
    """Score Latin-script text against the Hinglish / Kanglish lexicons."""
    words = {w.lower() for w in _WORD_RE.findall(text or "")}
    scores = {code: 0 for code in _ROMANISED_MARKERS}
    for code, markers in _ROMANISED_MARKERS.items():
        scores[code] = len(words & markers)
    return scores


def _normalise_text(text: str) -> str:
    """NFKC-normalise so composed/decomposed Indic input matches the same."""
    return unicodedata.normalize("NFKC", text or "")


def detect_language(text: str, fallback_language: str = DEFAULT_LANGUAGE) -> str:
    """Best-effort language code for `text`, restricted to supported codes.

    Understands native Devanagari/Kannada *and* romanised Hinglish/Kanglish.
    Defaults to `fallback_language` (English when unset) whenever the signal
    isn't clear — a wrong guess sends the user a reply they can't read, which
    is worse than falling back.
    """
    fallback = normalise_language(fallback_language)
    cleaned = _normalise_text(text).strip()
    if not cleaned:
        return fallback

    # 1. Native script decides outright.
    counts = script_counts(cleaned)
    if any(counts.values()):
        best = max(counts, key=lambda code: counts[code])
        if counts[best] > 0:
            return best

    # 2. Latin script: romanised lexicon scoring.
    letters = sum(1 for char in cleaned if char.isalpha())
    if letters:
        scores = score_romanised(cleaned)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_code, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if top_score >= _MIN_MARKER_SCORE and (
            top_score - runner_up
        ) >= _MIN_MARKER_MARGIN:
            logger.debug(
                "detect_language: romanised %s (score=%d, runner-up=%d)",
                top_code,
                top_score,
                runner_up,
            )
            return top_code

    # 3. Last resort: langdetect, only when there's enough text for it.
    detected = _langdetect(cleaned)
    if detected:
        return detected

    return fallback


def _langdetect(text: str) -> str | None:
    """Optional langdetect pass.  Returns a supported code or None."""
    words = text.split()
    if len(words) < 4:
        return None
    try:
        from langdetect import DetectorFactory, detect  # noqa: PLC0415
        from langdetect.lang_detect_exception import (  # noqa: PLC0415
            LangDetectException,
        )

        DetectorFactory.seed = 42
        detected = detect(text)
    except ImportError:
        logger.debug("langdetect not installed — skipping statistical pass")
        return None
    except LangDetectException:
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("langdetect failed: %s", exc)
        return None

    if detected in SUPPORTED_LANGUAGES:
        return detected
    # langdetect reports "mr" for Devanagari text that is often Hindi, and
    # "kn" is already handled by the script pass above.
    if detected == "mr":
        return "hi"
    return None


def is_menu_digit(text: str) -> bool:
    return (text or "").strip() in MENU_DIGITS


def is_language_request(text: str) -> bool:
    return (text or "").strip().lower() in LANG_KEYWORDS


def looks_like_feedback(text: str) -> int | None:
    return FEEDBACK_MAP.get((text or "").strip().lower())


def is_affirmative(text: str) -> bool:
    return (text or "").strip().lower() in YES_WORDS


def is_negative(text: str) -> bool:
    return (text or "").strip().lower() in NO_WORDS


def has_digits(text: str) -> bool:
    return bool(_DIGIT_RE.search(text or ""))
