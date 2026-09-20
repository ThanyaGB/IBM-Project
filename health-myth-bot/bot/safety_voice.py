"""Deterministic emergency circuit breaker + voice transcription.

check_emergency() is pure keyword matching with zero LLM calls and zero
network access — it must work even when every external service is down.

Matching rules
--------------
Plain entries are matched as literal phrases with word boundaries on both
sides, so a short word can never fire inside a longer one: "fit" no longer
matches "benefit" and "दर्द" no longer matches "सिरदर्द".  Entries prefixed
with "re:" are regexes, used where a qualifier can sit between the important
words ("सीने में *बहुत* दर्द", "ಎದೆಯಲ್ಲಿ *ತುಂಬಾ* ನೋವು").

Never add a bare symptom word ("fever", "pain", "headache", "ज्वर", "ज्वर",
"ಜ್ವರ", "ನೋವು"): mild versions of those are the most common questions this bot
receives, and a false emergency teaches users to ignore the real one.

Fever is the sharpest example, and the rule is deliberately stricter than it
first looks: **fever is never an emergency by itself, however emphatic.**
"my child has a very high fever, what should I do?" is the single most common
question this bot gets, and answering it with "call an ambulance" both hides
the real answer and trains the user to dismiss the warning.  The dangerous
presentations are caught by their red flags instead — convulsions, impaired
consciousness, breathing difficulty, stiff neck, bleeding — and the corpus
fact for fever already carries the "go to a health facility if above 39°C"
guidance, which is where that advice belongs.

Every language is matched regardless of the user's stored language, because
people code-switch under stress; the *reply* is then rendered in the user's
chosen language.

Transcription
-------------
transcribe_audio() tries, in order:

1. faster-whisper (local, offline, free, no per-minute cost)
2. openai-whisper (local, offline, free)
3. Gemini (free tier) — the only network path

The first available backend wins, so the bot still transcribes voice notes
when there is no API key and no internet.  Every backend failure is soft:
the caller gets an empty string and shows the "please type instead" reply.

download_audio() and transcribe_audio() wrap external I/O with soft-fail
semantics: exceptions are caught, logged, and the caller gets an empty
string rather than an unhandled 500.
"""

import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

# Cap for a single voice note.  WhatsApp voice notes are Opus at low bitrate,
# so 60 s is roughly 60 KB; this only rejects genuinely oversized uploads.
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", 16 * 1024 * 1024))

EMERGENCY_KEYWORDS: dict[str, list[str]] = {
    "en": [
        "chest pain",
        "chest ache",
        "chest tightness",
        "heart attack",
        "stiff neck",
        "febrile convulsion",
        "febrile convulsions",
        "severe bleeding",
        "heavy bleeding",
        "bleeding badly",
        "bleeding heavily",
        "won't stop bleeding",
        "difficulty breathing",
        "can't breathe",
        "cant breathe",
        "cannot breathe",
        "trouble breathing",
        "not breathing",
        "stopped breathing",
        "unconscious",
        "passed out",
        "unresponsive",
        "seizure",
        "convulsions",
        "convulsing",
        "having a fit",
        "poisoning",
        "poisoned",
        "swallowed poison",
        "swallowed bleach",
        "swallowed pills",
        "overdose",
        "stroke",
        "sudden numbness",
        "sudden weakness",
        "choking",
    ],
    "hi": [
        # Devanagari — regex entries tolerate a qualifier between the keywords
        "re:(?:सीने|सीना|छाती)(?:\\s+\\S+){0,3}?\\s+दर्द",  # chest pain
        "गर्दन में अकड़न",
        "गर्दन अकड़",
        "बेहोश",
        "बेहोशी",
        "re:होश\\s+(?:नहीं|नही)",
        "दौरा",
        "मिर्गी",
        "re:(?:सांस|साँस|सास)\\S*\\s+(?:नहीं|नही)",
        "re:दम\\s+(?:घुट|फूल)",
        "रक्तस्राव",
        "re:खून\\s+(?:बह|रुक)",
        "re:(?:बहुत|ज्यादा)\\s+खून",
        "जहर",
        "ज़हर",
        "ओवरडोज़",
        "ओवरडोज",
        "लकवा",
        "re:गला\\s+घुट",
        "हार्ट अटैक",
        "chest pain",
        "unconscious",
        "stroke",
        # Romanised Hinglish — typed in Latin script but still Hindi.
        # Regex entries tolerate a qualifier between the body part and the
        # symptom ("seene mein BAHUT dard"), exactly like the Devanagari ones.
        "re:seene?\\s+me(in)?\\s+(?:\\S+\\s+){0,3}?dard",
        "re:ch?hati?\\s+me(in)?\\s+(?:\\S+\\s+){0,3}?dard",
        "re:gardan\\s+(?:\\S+\\s+){0,2}?akad",  # stiff neck
        "gardan mein akadan",
        "re:sa(na|ans)\\s+nahi?n?",  # not breathing
        "saans lene mein takleef",
        "dam ghut",
        "gala ghut",
        "re:gala\\s+(?:\\S+\\s+){0,2}?ghut",
        "behosh",
        "behoshi",
        "hosh nahi",
        "daura",
        "mirgi",
        "re:bahut\\s+khoon",  # heavy bleeding
        "re:khoon\\s+(?:\\S+\\s+){0,2}?(?:beh|ruk)",
        "zeher",
        "zahar",
        "overdose",
        "lakwa",
    ],
    "kn": [
        # Kannada script — regex entries tolerate a qualifier between keywords
        # \S* after the body part because Kannada inflects by suffix
        # (ಎದೆ + ಯಲ್ಲಿ = ಎದೆಯಲ್ಲಿ, "in the chest") with no space between.
        "re:(?:ಎದೆ|ಎದೆಯ)\\S*(?:\\s+\\S+){0,3}?\\s+(?:ನೋವು|ನೋವು)",  # chest pain
        "ಕುತ್ತಿಗೆ ಬಿಗಿತ",
        "ಎಚ್ಚರ ತಪ್ಪಿದೆ",
        "ಎಚ್ಚರವಿಲ್ಲ",
        "ಪ್ರಜ್ಞೆ ತಪ್ಪಿದೆ",
        "ಪ್ರಜ್ಞೆ ಇಲ್ಲ",
        "ಮೂರ್ಛೆ",
        "ಅಪಸ್ಮಾರ",
        "ಸೆಳೆತ",
        "ಫಿಟ್ಸ್",
        "ಮಿರುಗಿ",
        "re:ಉಸಿರು\\s+(?:ಬರುತ್ತಿಲ್ಲ|ನಿಂತಿದೆ|ಕಟ್ಟಿದೆ|ಹೋಗುತ್ತಿಲ್ಲ)",
        "ಉಸಿರಾಡಲು ಕಷ್ಟ",
        "ಉಸಿರು ತೆಗೆದುಕೊಳ್ಳಲು ಕಷ್ಟ",
        "ರಕ್ತಸ್ರಾವ",
        "re:(?:ತುಂಬಾ|ಜಾಸ್ತಿ)\\s+ರಕ್ತ",
        "re:ರಕ್ತ\\s+(?:ನಿಲ್ಲುತ್ತಿಲ್ಲ|ನಿಂತಿಲ್ಲ|ಹರಿಯುತ್ತಿದೆ)",
        "ವಿಷ ಸೇವನೆ",
        "ವಿಷಪ್ರಾಶನ",
        "ಓವರ್ಡೋಸ್",
        "ಅತಿ ಸೇವನೆ",
        "ಪಾರ್ಶ್ವವಾಯು",
        "ಲಕ್ವಾ",
        "ಹೃದಯಾಘಾತ",
        "ಹಾರ್ಟ್ ಅಟ್ಯಾಕ್",
        "chest pain",
        "unconscious",
        "stroke",
        # Romanised Kanglish — Kannada typed in Latin script
        "ede novu",
        "ede novu ide",
        "yede novu",
        "ede nōvu",
        "kuttige bigita",
        "usiru baruttilla",
        "usiru nintide",
        "usiru kattide",
        "usirata kasta",
        "usiru togoloke kasta",
        "prajne tappide",
        "echchara tappide",
        "murche",
        "apasmara",
        "selata",
        "phits",
        "rakta srava",
        "tumba rakta",
        "rakta nilluttilla",
        "visha",
        "visha sevane",
        "overdose",
        "pashvavayu",
        "lakwa",
    ],
}

# Every language gets its own emergency reply: this is the one message that
# must be understood, so it is never sent in a language the user didn't pick.
# The Latin "EMERGENCY" header stays so the message is recognisable even if a
# user reads a different script than they type.
EMERGENCY_MESSAGES: dict[str, str] = {
    "en": (
        "⚠️ *This sounds like a medical emergency.*\n\n"
        "Please do one of the following *immediately*:\n"
        "• 🚑 Call your local emergency services (e.g., 112 / 911 / 999)\n"
        "• 🏥 Go to the nearest clinic or emergency room right now\n"
        "• 📞 Call a trained health worker or community health volunteer\n\n"
        "Do not wait. Your safety is the most important thing.\n\n"
        "_This bot is for general health information only and cannot replace "
        "emergency medical care._"
    ),
    "hi": (
        "🚨 *EMERGENCY / आपातकाल*\n\n"
        "कृपया *तुरंत* यह करें:\n"
        "• 🚑 112 / 108 पर कॉल करें (एम्बुलेंस)\n"
        "• 🏥 नजदीकी अस्पताल या क्लिनिक अभी जाएँ\n"
        "• 📞 किसी स्वास्थ्य कार्यकर्ता या आशा दीदी को बुलाएँ\n\n"
        "इंतज़ार न करें — _turant 112 par call karein, der na karein._\n\n"
        "_यह बॉट केवल सामान्य स्वास्थ्य जानकारी देता है और आपातकालीन "
        "इलाज का विकल्प नहीं है।_"
    ),
    "kn": (
        "🚨 *EMERGENCY / ತುರ್ತು ಪರಿಸ್ಥಿತಿ*\n\n"
        "ದಯವಿಟ್ಟು *ತಕ್ಷಣ* ಇವುಗಳಲ್ಲಿ ಒಂದನ್ನು ಮಾಡಿ:\n"
        "• 🚑 108 / 112 ಗೆ ಕರೆ ಮಾಡಿ (ಆಂಬ್ಯುಲೆನ್ಸ್)\n"
        "• 🏥 ಹತ್ತಿರದ ಆಸ್ಪತ್ರೆ ಅಥವಾ ಆರೋಗ್ಯ ಕೇಂದ್ರಕ್ಕೆ ಈಗಲೇ ಹೋಗಿ\n"
        "• 📞 ತರಬೇತಿ ಪಡೆದ ಆರೋಗ್ಯ ಕಾರ್ಯಕರ್ತರಿಗೆ ಕರೆ ಮಾಡಿ\n\n"
        "ಕಾಯಬೇಡಿ — _turant 108 ge call maadi, der maadbedi._\n\n"
        "_ಈ ಬಾಟ್ ಸಾಮಾನ್ಯ ಆರೋಗ್ಯ ಮಾಹಿತಿಗೆ ಮಾತ್ರ; ತುರ್ತು ಚಿಕಿತ್ಸೆಗೆ ಇದು "
        "ಬದಲಿಯಲ್ಲ._"
    ),
}

_APOSTROPHE_FIXES = {
    "\u2019": "'",  # right single quote
    "\u2018": "'",  # left single quote
    "\u02bc": "'",  # modifier letter apostrophe
    "\u200b": "",   # zero-width space
}

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_emergency_text(text: str) -> str:
    """Lowercase, fold apostrophes and collapse whitespace before matching."""
    cleaned = text or ""
    for source, target in _APOSTROPHE_FIXES.items():
        cleaned = cleaned.replace(source, target)
    return _WHITESPACE_RE.sub(" ", cleaned.lower()).strip()


def _compile_phrases(entries: list[str]) -> list[tuple[str, re.Pattern]]:
    compiled: list[tuple[str, re.Pattern]] = []
    for entry in entries:
        if entry.startswith("re:"):
            pattern = entry[3:]
        else:
            pattern = r"(?<!\w)" + re.escape(entry) + r"(?!\w)"
        compiled.append((entry, re.compile(pattern, re.IGNORECASE | re.UNICODE)))
    # Longest first so the most specific match is the one logged.
    compiled.sort(key=lambda item: len(item[0]), reverse=True)
    return compiled


_COMPILED_KEYWORDS: dict[str, list[tuple[str, re.Pattern]]] = {
    lang_code: _compile_phrases(phrases)
    for lang_code, phrases in EMERGENCY_KEYWORDS.items()
}


def get_emergency_message(language: str = "en") -> str:
    """Return the emergency reply in `language`, falling back to English."""
    return EMERGENCY_MESSAGES.get(language, EMERGENCY_MESSAGES["en"])


def match_emergency_phrase(text: str) -> tuple[str, str] | None:
    """Return (language, matched phrase) when `text` contains an emergency.

    Text is matched against *every* language, not just the user's stored one:
    a Kananda speaker may type in English, and a Hindi speaker may type
    "chest pain".
    """
    normalised = _normalise_emergency_text(text)
    if not normalised:
        return None
    for lang_code, patterns in _COMPILED_KEYWORDS.items():
        for entry, pattern in patterns:
            if pattern.search(normalised):
                return lang_code, entry
    return None


def check_emergency(text: str, language: str = "en") -> tuple[bool, str | None]:
    """Return (is_emergency, reply_in_user_language).

    The reply is always produced in the caller's `language` — a user who
    cannot read English must not receive an English-only emergency message.
    """
    match = match_emergency_phrase(text)
    if match is None:
        return False, None
    lang_code, entry = match
    logger.warning("Emergency phrase detected (%s): %r", lang_code, entry)
    return True, get_emergency_message(language)


def download_audio(media_url: str, dest_path: str, auth: tuple | None = None,
                   headers: dict | None = None, timeout: int = 30) -> str:
    """Stream `media_url` to `dest_path`.

    `auth` is a (user, token) tuple for HTTP-basic providers (Twilio);
    `headers` carries a bearer token for providers that use one (WhatsApp
    Cloud API).  Raises on HTTP error, and removes the partial file so a
    failed download never leaves a truncated temp file behind.
    """
    try:
        response = requests.get(
            media_url, auth=auth, headers=headers, timeout=timeout, stream=True
        )
        response.raise_for_status()

        written = 0
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                written += len(chunk)
                if written > MAX_AUDIO_BYTES:
                    raise ValueError(
                        f"audio exceeds MAX_AUDIO_BYTES ({MAX_AUDIO_BYTES})"
                    )
                fh.write(chunk)
    except Exception:
        safe_delete(dest_path)
        raise

    logger.info(
        "Audio downloaded to %s (%d bytes)", dest_path, os.path.getsize(dest_path)
    )
    return dest_path


# ---------------------------------------------------------------------------
# Transcription backends
# ---------------------------------------------------------------------------
# Two-letter codes as the Whisper family expects them.
_WHISPER_LANG = {"en": "en", "hi": "hi", "kn": "kn"}


def _transcribe_faster_whisper(audio_file_path: str, language: str) -> str | None:
    """Local, offline STT.  Returns None when the backend isn't available."""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError:
        return None

    model_name = os.environ.get("WHISPER_MODEL", "base")
    try:
        model = _faster_whisper_model(model_name)
        segments, info = model.transcribe(
            audio_file_path,
            language=_WHISPER_LANG.get(language),
            beam_size=1,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        logger.info(
            "faster-whisper transcription (%s, %s), %d chars",
            model_name,
            getattr(info, "language", "?"),
            len(text),
        )
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("faster-whisper transcription failed: %s", exc)
        return None


_FASTER_WHISPER_CACHE: dict[str, object] = {}


def _faster_whisper_model(model_name: str):
    """Load (and memoise) a faster-whisper model.

    Loading is expensive — seconds and hundreds of MB — so one instance is
    kept per process.
    """
    if model_name not in _FASTER_WHISPER_CACHE:
        from faster_whisper import WhisperModel  # noqa: PLC0415

        _FASTER_WHISPER_CACHE[model_name] = WhisperModel(
            model_name,
            device=os.environ.get("WHISPER_DEVICE", "cpu"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
        )
    return _FASTER_WHISPER_CACHE[model_name]


def _transcribe_openai_whisper(audio_file_path: str, language: str) -> str | None:
    """Local openai-whisper fallback.  Returns None when unavailable."""
    try:
        import whisper  # noqa: PLC0415
    except ImportError:
        return None

    try:
        model = whisper.load_model(os.environ.get("WHISPER_MODEL", "base"))
        result = model.transcribe(
            audio_file_path, language=_WHISPER_LANG.get(language), fp16=False
        )
        text = (result.get("text") or "").strip()
        logger.info("openai-whisper transcription, %d chars", len(text))
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("openai-whisper transcription failed: %s", exc)
        return None


def _transcribe_gemini(audio_file_path: str, language: str) -> str | None:
    """Gemini free-tier STT — the only network path.  None when unavailable."""
    try:
        import google.genai  # noqa: PLC0415
    except ImportError:
        return None

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.info("GEMINI_API_KEY not set — skipping Gemini transcription")
        return None

    try:
        import mimetypes  # noqa: PLC0415

        with open(audio_file_path, "rb") as fh:
            audio_bytes = fh.read()

        mime_type, _ = mimetypes.guess_type(audio_file_path)
        if mime_type is None:
            mime_type = "audio/ogg"

        client = google.genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_TRANSCRIBE_MODEL", "gemini-3.5-flash-lite")
        response = client.models.generate_content(
            model=model_name,
            contents=[
                {"inline_data": {"mime_type": mime_type, "data": audio_bytes}},
                {
                    "text": (
                        "Transcribe the speech in this audio file. The speaker "
                        "is asking a health question, possibly in English, "
                        "Hindi or Kannada. Provide only the transcribed text "
                        "in its original script, nothing else."
                    )
                },
            ],
        )
        text = response.text.strip() if response.text else ""
        logger.info("Gemini transcription, %d chars", len(text))
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Gemini transcription failed: %s", exc)
        return None


def transcribe_audio(audio_file_path: str, language: str = "en") -> str:
    """Transcribe a voice note, trying free local backends before the cloud.

    Always deletes the audio file, whatever happens, so a failed voice note
    can never leak a temp file.  Returns "" when every backend fails.
    """
    if not os.path.exists(audio_file_path):
        logger.warning("transcribe_audio: %s does not exist", audio_file_path)
        return ""

    try:
        for backend, name in (
            (_transcribe_faster_whisper, "faster-whisper"),
            (_transcribe_openai_whisper, "openai-whisper"),
            (_transcribe_gemini, "gemini"),
        ):
            text = backend(audio_file_path, language)
            if text:
                return text
            logger.debug("%s backend unavailable or returned nothing", name)
        logger.error("All transcription backends failed for %s", audio_file_path)
        return ""
    finally:
        safe_delete(audio_file_path)


def safe_delete(path: str) -> None:
    """Delete `path` if it exists, never raising.

    Public so callers can clean up temp files on paths where an earlier step
    (e.g. downloading a voice note) failed before transcription ever ran.
    """
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


_safe_delete = safe_delete
