"""Flask webhook server for the health-myth-bot WhatsApp integration.

POST /webhook — Twilio WhatsApp message handler.
GET  /health  — simple uptime probe.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_REQUIRED_VARS = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
]
_missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
if _missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        "Copy .env.example to .env and fill in real values."
    )

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))

import database  # noqa: E402
import rag_engine  # noqa: E402
import safety_voice  # noqa: E402
from card_generator import generate_myth_card  # noqa: E402

_FACTS_PATH = Path(os.environ.get("HEALTH_FACTS_PATH", "health_facts.json"))
try:
    with _FACTS_PATH.open("r", encoding="utf-8") as _fh:
        _HEALTH_FACTS: list[dict] = json.load(_fh)
except Exception as _exc:  # pylint: disable=broad-except
    logger.warning("Could not load health_facts.json for category lookup: %s", _exc)
    _HEALTH_FACTS = []

_CATEGORY_MAP: dict[str, str] = {}
for _fact in _HEALTH_FACTS:
    for _kw in _fact["topic"].lower().split():
        _CATEGORY_MAP[_kw] = _fact["category"]
    _CATEGORY_MAP[_fact["category"].lower()] = _fact["category"]

database.init_db()
rag_engine.init_vector_store()

app = Flask(__name__)

LANG_MAP = {"1": "en", "2": "hi", "3": "sw"}
LANG_KEYWORDS = {"language", "भाषा", "lugha"}

LANG_PROMPTS = {
    "en": "Please reply with a number to choose your language:\n1 – English\n2 – हिन्दी (Hindi)\n3 – Kiswahili (Swahili)",
    "hi": "कृपया अपनी भाषा चुनने के लिए एक नंबर से उत्तर दें:\n1 – English\n2 – हिन्दी (Hindi)\n3 – Kiswahili (Swahili)",
    "sw": "Tafadhali jibu na nambari kuchagua lugha yako:\n1 – English\n2 – हिन्दी (Hindi)\n3 – Kiswahili (Swahili)",
}

LANG_CONFIRMATIONS = {
    "en": "✅ Language set to *English*. Ask me any health question!",
    "hi": "✅ भाषा *हिन्दी* पर सेट की गई। मुझसे कोई भी स्वास्थ्य प्रश्न पूछें!",
    "sw": "✅ Lugha imewekwa kwa *Kiswahili*. Niulize swali lolote la afya!",
}

FEEDBACK_PROMPT = {
    "en": "💬 Was this helpful? Reply 👍 or 👎",
    "hi": "💬 क्या यह उपयोगी था? 👍 या 👎 जवाब दें",
    "sw": "💬 Je, hii ilisaidia? Jibu 👍 au 👎",
}

FEEDBACK_ACK = {
    "en": "Thank you for your feedback! 🙏",
    "hi": "आपकी प्रतिक्रिया के लिए धन्यवाद! 🙏",
    "sw": "Asante kwa maoni yako! 🙏",
}

FLAG_PROMPT = {
    "en": "This topic isn't in our verified database yet. Would you like to flag it for review by health officials? Reply *YES* to report.",
    "hi": "यह विषय अभी हमारे सत्यापित डेटाबेस में नहीं है। क्या आप इसे स्वास्थ्य अधिकारियों की समीक्षा के लिए फ्लैग करना चाहेंगे? रिपोर्ट करने के लिए *YES* जवाब दें।",
    "sw": "Mada hii bado haipo katika hifadhidata yetu iliyothibitishwa. Je, ungependa kuiwasilisha kwa ukaguzi wa maafisa wa afya? Jibu *YES* kuripoti.",
}

FLAG_ACK = {
    "en": "✅ Reported! Our health team will review it. Thank you for helping improve our database. 🙏",
    "hi": "✅ रिपोर्ट हो गई! हमारी स्वास्थ्य टीम इसकी समीक्षा करेगी। हमारे डेटाबेस को बेहतर बनाने में मदद के लिए धन्यवाद। 🙏",
    "sw": "✅ Imeripotiwa! Timu yetu ya afya itaifanyia ukaguzi. Asante kwa kusaidia kuboresha hifadhidata yetu. 🙏",
}

ERROR_REPLY = "⚠️ Something went wrong on our end. Please try again in a moment. If this persists, contact your local health centre."
AUDIO_FAILED_REPLY = "I wasn't able to process your voice note. Could you please type your question instead? 🙏"

_PENDING_FLAG: dict[str, str] = {}      # phone -> query awaiting YES/NO to flag
_PENDING_FEEDBACK: dict[str, bool] = {}  # phone -> awaiting feedback (True)


def _guess_category(query: str) -> str:
    lower = query.lower()
    for kw, cat in _CATEGORY_MAP.items():
        if kw in lower:
            return cat
    return "General"


def _twiml_reply(message: str, media_url: str | None = None) -> str:
    resp = MessagingResponse()
    msg = resp.message(message)
    if media_url:
        msg.media(media_url)
    return str(resp)


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        return _handle_message()
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unhandled exception in /webhook: %s", exc)
        return _twiml_reply(ERROR_REPLY), 200


def _handle_message() -> tuple[str, int]:
    phone_number: str = request.form.get("From", "unknown")
    body: str = (request.form.get("Body") or "").strip()
    media_url: str | None = request.form.get("MediaUrl0")
    media_content_type: str | None = request.form.get("MediaContentType0")

    # Audio transcription (if voice note present)
    if media_url and media_content_type and "audio" in media_content_type:
        logger.info("Audio message received from %s", phone_number[:6] + "XXXXXX")
        suffix = ".ogg"
        if "mpeg" in media_content_type or "mp4" in media_content_type:
            suffix = ".mp4"
        elif "wav" in media_content_type:
            suffix = ".wav"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name

        try:
            safety_voice.download_audio(
                media_url=media_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                dest_path=tmp_path,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Audio download failed: %s", exc)
            return _twiml_reply(AUDIO_FAILED_REPLY), 200

        transcript = safety_voice.transcribe_audio(tmp_path)
        if not transcript:
            return _twiml_reply(AUDIO_FAILED_REPLY), 200

        body = transcript
        logger.info("Transcript: %s...", body[:80])

    # Emergency check — must happen before any LLM call
    is_emergency, emergency_msg = safety_voice.check_emergency(body)
    if is_emergency:
        database.log_query(
            phone_number=phone_number,
            query_text=body,
            language=database.get_user_language(phone_number) or "en",
            category="Emergency",
            is_emergency=True,
        )
        return _twiml_reply(emergency_msg), 200

    # Feedback response handling (👍 / 👎)
    if _PENDING_FEEDBACK.get(phone_number):
        feedback_map = {
            "👍": 1, "👎": -1, "1": 1, "-1": -1,
            "thumbs up": 1, "thumbs down": -1, "good": 1, "bad": -1,
        }
        rating = feedback_map.get(body.strip().lower())
        if rating is not None:
            database.record_feedback(phone_number=phone_number, rating=rating)
            _PENDING_FEEDBACK.pop(phone_number, None)
            lang = database.get_user_language(phone_number) or "en"
            return _twiml_reply(FEEDBACK_ACK.get(lang, FEEDBACK_ACK["en"])), 200
        _PENDING_FEEDBACK.pop(phone_number, None)

    # Flagging confirmation (YES / NO)
    if phone_number in _PENDING_FLAG:
        pending_query = _PENDING_FLAG.pop(phone_number)
        lang = database.get_user_language(phone_number) or "en"
        if body.strip().upper() == "YES":
            database.flag_myth(query_text=pending_query, language=lang)
            return _twiml_reply(FLAG_ACK.get(lang, FLAG_ACK["en"])), 200
        return _twiml_reply(
            "Understood. Feel free to ask another question any time! 😊"
        ), 200

    # Language change keyword
    if body.strip().lower() in LANG_KEYWORDS:
        lang = database.get_user_language(phone_number) or "en"
        return _twiml_reply(LANG_PROMPTS.get(lang, LANG_PROMPTS["en"])), 200

    # Language selection digit (1/2/3)
    if body.strip() in LANG_MAP:
        new_lang = LANG_MAP[body.strip()]
        database.set_user_language(phone_number, new_lang)
        return _twiml_reply(LANG_CONFIRMATIONS[new_lang]), 200

    # First-message language detection
    stored_language = database.get_user_language(phone_number)

    if stored_language is None:
        detected = rag_engine.detect_language(text=body, fallback_language="en")
        if detected != "en" or len(body.split()) >= 3:
            database.set_user_language(phone_number, detected)
            stored_language = detected
        else:
            return _twiml_reply(LANG_PROMPTS["en"]), 200

    language = stored_language

    # RAG myth rebuttal
    rebuttal = rag_engine.get_myth_rebuttal(query=body, target_language=language)
    is_fallback = _is_fallback_response(rebuttal)

    category = _guess_category(body)
    database.log_query(
        phone_number=phone_number,
        query_text=body,
        language=language,
        category=category,
        is_emergency=False,
    )

    # "Report a new myth" intake
    if is_fallback:
        _PENDING_FLAG[phone_number] = body
        flag_prompt = FLAG_PROMPT.get(language, FLAG_PROMPT["en"])
        full_reply = f"{rebuttal}\n\n{flag_prompt}"
        return _twiml_reply(full_reply), 200

    # Try to send a shareable verified card
    card_media_url: str | None = None
    best_fact = _find_best_fact_record(body)
    if best_fact:
        try:
            card_path = generate_myth_card(best_fact)
            card_media_url = os.environ.get("CARD_BASE_URL", "")
            if card_media_url:
                card_media_url = card_media_url.rstrip("/") + "/" + Path(card_path).name
            else:
                card_media_url = None
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Card generation failed (non-fatal): %s", exc)
            card_media_url = None

    # Final reply with feedback prompt
    feedback_p = FEEDBACK_PROMPT.get(language, FEEDBACK_PROMPT["en"])
    full_reply = f"{rebuttal}\n\n{feedback_p}"
    _PENDING_FEEDBACK[phone_number] = True
    return _twiml_reply(full_reply, media_url=card_media_url), 200


_FALLBACK_MARKERS = [
    "couldn't find a verified answer",
    "sijaona jibu lililothibitishwa",
    "सत्यापित उत्तर नहीं मिला",
]


def _is_fallback_response(text: str) -> bool:
    lower = text.lower()
    return any(marker.lower() in lower for marker in _FALLBACK_MARKERS)


def _find_best_fact_record(query: str) -> dict | None:
    if not _HEALTH_FACTS:
        return None
    lower = query.lower()
    for fact in _HEALTH_FACTS:
        for kw in fact["topic"].lower().split():
            if kw in lower:
                return fact
    return None


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "health-myth-bot"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
