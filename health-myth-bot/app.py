"""Flask webhook server for the health-myth-bot WhatsApp integration.

Routes
------
``GET  /``        — the landing page with the live phone-emulator demo (demo.py).
``GET  /webhook`` — provider verification handshake (WhatsApp Cloud API).
``POST /webhook`` — inbound message handler; the channel adapter validates the
                    request, normalises it, and sends the reply.
``GET  /health``  — uptime probe plus an honest inventory of what is
                    configured, degraded, or disabled.

Design notes
------------
**Channel-agnostic.**  All provider specifics live in ``channels.py``.  This
module deals in ``InboundMessage`` / ``OutboundReply`` and nothing else, so
moving from Twilio to the Cloud API is a config change (``CHANNEL=twilio``)
rather than a rewrite.

**Free by construction.**  Every outgoing message here is a reply to an inbound
message, sent inside the 24-hour customer-service window that message opened —
inbound is never charged and non-template replies inside the window are free.
Nothing in this file sends a template, and ``advisory.py`` refuses to send to
anyone outside their window.  That is the cost control: not a budget, an
architecture.

**Fail loudly, reply anyway.**  A missing credential or a dead backend is
logged at error level and reported by ``/health``; the user still gets either a
real answer or the localised "consult your health worker" fallback.  The one
exception is a missing webhook secret, which fails *closed* — an unauthenticated
webhook lets anyone inject messages into the bot.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

FLASK_PORT = int(os.environ.get("FLASK_PORT", 5000))

# Per-user message cap.  A person asking more than this in an hour is either in
# distress or is a script; either way the free tier and the human both benefit
# from a pause.  Counted from the logs table, so it survives a restart.
DEFAULT_RATE_LIMIT_PER_HOUR = 20

# Voice replies are only worth the extra round trip when the user sent a voice
# note — they chose to speak, so they get spoken answers.
VOICE_REPLIES_DEFAULT = os.environ.get("VOICE_REPLIES", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Interactive button payload ids.  A tapped button arrives as its id, so these
# have to be resolved before the text handlers run.
BUTTON_FEEDBACK_UP = "feedback_up"
BUTTON_FEEDBACK_DOWN = "feedback_down"
BUTTON_FLAG_YES = "flag_yes"
BUTTON_FLAG_NO = "flag_no"
BUTTON_LANG_PREFIX = "lang_"

# Sender prefix reserved for website-demo sessions (see demo.py).  The demo
# endpoint builds senders from it so a demo conversation can never collide
# with — and can always be told apart from — a real phone number.
DEMO_SENDER_PREFIX = "demo:"

_boot_warnings: list[str] = []


def boot_warnings() -> list[str]:
    return list(_boot_warnings)


def _mask(phone_number: str) -> str:
    """Mask a phone number for logs.

    Truncating the front ([:6]) logged 'whatsa' for every Twilio number —
    useless for debugging and still identifying in aggregate.  Keep the country
    code and the last two digits, mask the middle.
    """
    digits = "".join(ch for ch in phone_number if ch.isdigit())
    if len(digits) <= 4:
        return "***"
    return f"+{digits[:2]}…{digits[-2:]}"


def _display(sender: str) -> str:  # backward-compatible alias used in tests
    return _mask(sender)


def _require_at_least_one_generator() -> None:
    """Warn (loudly) when no answer backend is configured.

    Missing generation is a degraded mode, not a crash: the bot still answers
    with the localised "consult your health worker" text and the dashboard shows
    red.  Crashing on boot instead would take the emergency keyword path down
    with it — and that path must keep working when everything else is broken.
    """
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("OLLAMA_BASE_URL"):
        return
    message = (
        "No answer backend configured: set GEMINI_API_KEY (free tier) or "
        "OLLAMA_BASE_URL (local, free). Every reply will be the fallback message."
    )
    _boot_warnings.append(message)
    logger.error(message)


def _verify_channel_credentials() -> None:
    """Soft-fail credential check for the configured channel.

    A wrong token should be visible at boot, because it otherwise only shows up
    when a voice note silently fails to arrive.
    """
    channel_name = os.environ.get("CHANNEL", "whatsapp").strip().lower()
    if channel_name in {"whatsapp", "cloud", "meta"}:
        missing = [
            var
            for var in (
                "WHATSAPP_ACCESS_TOKEN",
                "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_APP_SECRET",
                "WHATSAPP_VERIFY_TOKEN",
            )
            if not os.environ.get(var)
        ]
        if missing:
            message = (
                f"WhatsApp Cloud API is missing {', '.join(missing)}. "
                "See .env.example — without WHATSAPP_APP_SECRET the webhook "
                "rejects every request (it cannot be authenticated)."
            )
            _boot_warnings.append(message)
            logger.error(message)
        else:
            logger.info("WhatsApp Cloud API credentials present")
    else:
        missing = [
            var
            for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
            if not os.environ.get(var)
        ]
        if missing:
            message = f"Twilio is missing {', '.join(missing)}"
            _boot_warnings.append(message)
            logger.error(message)


_require_at_least_one_generator()
_verify_channel_credentials()


from bot import channels as channels_module  # noqa: E402
from bot import database  # noqa: E402
from bot import rag_engine  # noqa: E402
from bot import retrieval  # noqa: E402
from bot import safety_voice  # noqa: E402
from bot import tts  # noqa: E402
from bot.card_generator import generate_myth_card  # noqa: E402
from bot.language import (  # noqa: E402
    AUDIO_FAILED_REPLY,
    BOT_PAUSED_REPLY,
    ERROR_REPLY,
    FEEDBACK_ACK,
    FEEDBACK_PROMPT,
    FLAG_ACK,
    FLAG_DECLINED,
    FLAG_PROMPT,
    LANG_CONFIRMATIONS,
    LANG_PROMPTS,
    MENU_DIGITS,
    RATE_LIMITED_REPLY,
    get_string,
    is_affirmative,
    is_language_request,
    is_menu_digit,
    is_negative,
    looks_like_feedback,
    normalise_language,
)

database.init_db()
rag_engine.init_retriever()

app = Flask(__name__)

# Landing page + website demo emulator (serves /, /demo/config, /demo/message).
# Imported after init_db()/init_retriever() above so the demo endpoint can
# never answer before the corpus and database are ready.
import demo as demo_module  # noqa: E402

demo_module.registerDemoBlueprint(app)


# ---------------------------------------------------------------------------
# Reply construction
# ---------------------------------------------------------------------------
def _feedback_buttons() -> list[tuple[str, str]]:
    # WhatsApp button titles are capped at 20 characters.
    return [(BUTTON_FEEDBACK_UP, "👍 Helpful"), (BUTTON_FEEDBACK_DOWN, "👎 Not helpful")]


def _flag_buttons() -> list[tuple[str, str]]:
    return [(BUTTON_FLAG_YES, "Yes, report it"), (BUTTON_FLAG_NO, "No thanks")]


def _answer_with_card(
    text: str, query: str, language: str, *, with_buttons: bool = True
) -> channels_module.OutboundReply:
    """Assemble a reply, attaching a verified card when one is warranted."""
    image_path: str | None = None
    facts = retrieval.search(query, top_k=1)
    if facts:
        try:
            image_path = generate_myth_card(facts[0].record, language)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Card generation failed (non-fatal): %s", exc)
            image_path = None
    return channels_module.OutboundReply(
        text=text,
        image_path=image_path,
        buttons=_feedback_buttons() if with_buttons else [],
    )


def rate_limit_per_hour() -> int:
    """Read at call time, so an operator can retune it without a redeploy."""
    try:
        return int(os.environ.get("RATE_LIMIT_PER_HOUR", DEFAULT_RATE_LIMIT_PER_HOUR))
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_HOUR


def _voice_reply(text: str, language: str) -> str | None:
    """A spoken version of `text`, when the user sent a voice note."""
    if not VOICE_REPLIES_DEFAULT:
        return None
    return tts.synthesize(text, language)


# ---------------------------------------------------------------------------
# Inbound handling
# ---------------------------------------------------------------------------
def _resolve_button(text: str) -> tuple[str, str | None]:
    """Map a tapped-button id (or its title) to an intent.

    Returns ``(intent, payload)`` where intent is one of
    ``feedback_up``, ``feedback_down``, ``flag_yes``, ``flag_no``, ``lang``,
    or ``text``.
    """
    raw = (text or "").strip()
    lowered = raw.lower()

    if raw in {BUTTON_FEEDBACK_UP, BUTTON_FEEDBACK_DOWN} or lowered in {
        "👍 helpful",
        "👎 not helpful",
        "helpful",
        "not helpful",
    }:
        return (
            "feedback_up"
            if raw == BUTTON_FEEDBACK_UP or "helpful" in lowered and "not" not in lowered
            else "feedback_down",
            None,
        )

    if raw in {BUTTON_FLAG_YES, BUTTON_FLAG_NO} or lowered in {
        "yes, report it",
        "no thanks",
    }:
        return ("flag_yes" if raw == BUTTON_FLAG_YES or lowered.startswith("yes") else "flag_no", None)

    if lowered.startswith(BUTTON_LANG_PREFIX):
        return "lang", lowered.removeprefix(BUTTON_LANG_PREFIX)

    return "text", None


def _download_and_transcribe(
    message: channels_module.InboundMessage,
    channel: channels_module.Channel,
    language: str,
) -> tuple[str, bool]:
    """Download and transcribe an inbound voice note.

    Returns ``(transcript, ok)``.  The temp file is created *after* the
    download target is known and always cleaned up, so a failed voice note
    cannot leak a file — the bug in the previous ordering, where the file
    existed before the download and the early return skipped cleanup.
    """
    suffix = channels_module.audio_suffix_for(message.media_type)
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.close()
    tmp_path = handle.name

    try:
        channel.download_media(message, tmp_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Audio download failed: %s", exc)
        safety_voice.safe_delete(tmp_path)
        return "", False

    # transcribe_audio() removes the file in its own finally block.
    transcript = safety_voice.transcribe_audio(tmp_path, language)
    if not transcript:
        safety_voice.safe_delete(tmp_path)
        return "", False
    return transcript, True


def _handle_inbound(
    message: channels_module.InboundMessage,
    channel: channels_module.Channel,
    *,
    log_queries: bool = True,
) -> channels_module.OutboundReply | None:
    """Produce one reply for one inbound message (None = send nothing).

    ``log_queries=False`` runs the full pipeline but skips the two
    ``database.log_query`` writes (emergency and normal), so a website demo
    can exercise the real answering path without writing analytics rows for a
    synthetic user.  Anything the demo *taps* — feedback and myth reports —
    still writes, on purpose: nobody but a real person monitoring the demo can
    produce those, so they are signal.
    """
    sender = message.sender
    language = normalise_language(database.get_user_language(sender))

    # The user's own message opens their free reply window.  Recording it here,
    # for every inbound message, is what lets advisory.py stay free.
    database.open_service_window(sender)

    # 1. Emergency first, always, before any LLM call or rate limit.  Someone
    #    in trouble must never be told to slow down.
    if message.text:
        # The emergency reply must match the language of the *message*, not
        # just the session: a first-time user typing an emergency gets no
        # second chance to pick a language.  Detection falls back to the
        # stored language when the text is ambiguous.
        reply_language = language
        if database.get_user_language(sender) is None:
            reply_language = normalise_language(
                rag_engine.detect_language(text=message.text, fallback_language=language)
            )
        is_emergency, emergency_msg = safety_voice.check_emergency(
            message.text, language=reply_language
        )
        if is_emergency:
            if log_queries:
                database.log_query(
                    phone_number=sender,
                    query_text=message.text,
                    language=reply_language,
                    category="Emergency",
                    is_emergency=True,
                )
            return channels_module.OutboundReply(
                text=emergency_msg or "", buttons=[]
            )

    # 2. Kill switch.  Checked before anything expensive, so pausing really
    #    pauses rather than only hiding output.
    if not database.bot_enabled():
        logger.info("Bot disabled — replying with the paused notice")
        return channels_module.OutboundReply(
            text=get_string(BOT_PAUSED_REPLY, language), buttons=[]
        )

    if database.count_recent_queries(sender, hours=1) >= rate_limit_per_hour():
        logger.warning("Rate limit hit for %s", _mask(sender))
        return channels_module.OutboundReply(
            text=get_string(RATE_LIMITED_REPLY, language), buttons=[]
        )

    intent, payload = _resolve_button(message.text)

    # 3. Voice note → text.
    was_voice = message.has_audio
    if was_voice:
        logger.info("Voice note from %s", _mask(sender))
        transcript, ok = _download_and_transcribe(message, channel, language)
        if not ok:
            return channels_module.OutboundReply(
                text=get_string(AUDIO_FAILED_REPLY, language), buttons=[]
            )
        message.text = transcript
        logger.info("Transcript: %s…", transcript[:80])

    body = (message.text or "").strip()

    # 4. Pending feedback.
    if database.get_pending_state(sender, "feedback") is not None:
        rating = looks_like_feedback(body)
        if intent == "feedback_up":
            rating = 1
        elif intent == "feedback_down":
            rating = -1
        if rating is not None:
            database.record_feedback(phone_number=sender, rating=rating)
            database.clear_pending_state(sender, "feedback")
            return channels_module.OutboundReply(
                text=get_string(FEEDBACK_ACK, language), buttons=[]
            )
        # Not feedback after all: drop the pending state and treat this as a
        # fresh question, rather than swallowing it as a no.
        database.clear_pending_state(sender, "feedback")

    # 5. Pending flag confirmation.
    pending_query = database.get_pending_state(sender, "flag")
    if pending_query is not None:
        database.clear_pending_state(sender, "flag")
        if intent == "flag_yes" or is_affirmative(body):
            database.flag_myth(query_text=pending_query, language=language)
            return channels_module.OutboundReply(
                text=get_string(FLAG_ACK, language), buttons=[]
            )
        if intent == "flag_no" or is_negative(body):
            return channels_module.OutboundReply(
                text=get_string(FLAG_DECLINED, language), buttons=[]
            )
        # Anything else is a new question, not an answer to ours.  Falling
        # through (rather than consuming it as a "no") is what stops a pending
        # prompt from swallowing the user's next real question.

    # 6. Language menu: keyword, button, or digit.
    if is_language_request(body):
        return channels_module.OutboundReply(
            text=get_string(LANG_PROMPTS, language), buttons=[]
        )
    if intent == "lang" and payload in MENU_DIGITS:
        return _set_language(sender, MENU_DIGITS[payload])
    if is_menu_digit(body):
        return _set_language(sender, MENU_DIGITS[body.strip()])

    # 7. First contact: detect language, or ask.
    stored = database.get_user_language(sender)
    if stored is None:
        detected = rag_engine.detect_language(text=body, fallback_language="en")
        if detected != "en" or len(body.split()) >= 3:
            database.set_user_language(sender, detected)
            language = detected
        else:
            # One or two ambiguous Latin words.  Asking beats guessing wrong.
            return channels_module.OutboundReply(
                text=get_string(LANG_PROMPTS, "en"), buttons=[]
            )

    # 8. Answer.
    rebuttal = rag_engine.get_myth_rebuttal(query=body, target_language=language)
    is_fallback = _is_fallback_response(rebuttal)

    if log_queries:
        database.log_query(
            phone_number=sender,
            query_text=body,
            language=language,
            category=retrieval.get_retriever().category_for(body),
            is_emergency=False,
        )

    if is_fallback:
        # "Report a new myth" intake, with buttons so the user doesn't have to
        # type YES.
        database.set_pending_state(sender, "flag", payload=body)
        return channels_module.OutboundReply(
            text=f"{rebuttal}\n\n{get_string(FLAG_PROMPT, language)}",
            buttons=_flag_buttons(),
        )

    database.set_pending_state(sender, "feedback", payload="awaiting")
    reply = _answer_with_card(
        f"{rebuttal}\n\n{get_string(FEEDBACK_PROMPT, language)}", body, language
    )
    if was_voice:
        reply.audio_path = _voice_reply(rebuttal, language)
    return reply


def _set_language(sender: str, new_language: str) -> channels_module.OutboundReply:
    database.set_user_language(sender, new_language)
    return channels_module.OutboundReply(
        text=get_string(LANG_CONFIRMATIONS, new_language), buttons=[]
    )


def _is_fallback_response(text: str) -> bool:
    from bot.language import is_fallback_response  # noqa: PLC0415

    return is_fallback_response(text)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    channel = channels_module.get_channel()

    authorised, challenge = channel.verify(request)
    if not authorised:
        return ("", 403)
    if request.method == "GET":
        # Cloud API verification handshake: echo the challenge back verbatim.
        return (challenge or "", 200)

    messages = channel.parse(request)
    if not messages:
        # A delivery receipt or a status callback.  Providers expect a 200.
        return ("", 200)

    for message in messages:
        # Providers retry webhooks.  Handling the same message twice would
        # re-run the LLM and double-log the question.
        if message.message_id and database.is_message_processed(message.message_id):
            logger.info("Duplicate delivery %s — ignoring", message.message_id)
            continue  # pragma: no cover - exercised in tests

        try:
            reply = _handle_inbound(message, channel)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Unhandled exception handling message: %s", exc)
            language = normalise_language(database.get_user_language(message.sender))
            reply = channels_module.OutboundReply(
                text=get_string(ERROR_REPLY, language), buttons=[]
            )

        if message.message_id:
            database.mark_message_processed(message.message_id)

        if reply is None:
            continue

        # A send failure is not the user's problem to see; it is ours to log.
        if not channel.send(message.sender, reply):
            logger.error("Reply delivery failed for %s", _mask(message.sender))

    # An empty 200 is the correct acknowledgement: replies go out through the
    # API, which keeps this route usable for both channels and for a browser
    # that is only verifying the endpoint.
    return ("", 200)


@app.route("/health", methods=["GET"])
def health():
    """Uptime probe plus what is actually configured."""
    from bot.card_renderer import renderer_status  # noqa: PLC0415

    retrieval_stats: dict = {}
    try:
        retrieval_stats = retrieval.get_retriever().stats()
    except Exception as exc:  # pylint: disable=broad-except
        retrieval_stats = {"error": str(exc)}

    degraded = [
        name
        for name, ok in (
            ("answer_backend", rag_engine.status()["gemini_configured"]
             or rag_engine.status()["ollama_configured"]),
            ("indic_card_shaping", renderer_status()["shapes_indic_correctly"]),
            ("voice_replies", tts.status()["available"]),
            ("bot_enabled", database.bot_enabled()),
        )
        if not ok
    ]

    return (
        jsonify(
            {
                "status": "ok" if not degraded else "degraded",
                "service": "health-myth-bot",
                "channel": channels_module.get_channel().name,
                "retrieval": retrieval_stats,
                "generation": rag_engine.status(),
                "cards": renderer_status(),
                "voice": tts.status(),
                "bot_enabled": database.bot_enabled(),
                "degraded": degraded,
                "boot_warnings": boot_warnings(),
            }
        ),
        200,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
