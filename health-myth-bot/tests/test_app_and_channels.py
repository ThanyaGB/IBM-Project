"""Webhook flows, channel adapters, and the free-window guarantee.

The route is driven with a recording channel, so these tests exercise the whole
path — route → idempotency → core handler → reply assembly → send — without
standing up a provider.  Provider-specific parsing, signature validation and
payload building are then tested directly against the adapters.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile

import pytest

import app as app_module
from bot import channels as channels_module
from bot import database
from bot import language
from bot import retrieval
from bot import safety_voice

SENDER = "whatsapp:+919999000001"


class RecordingChannel:
    """A channel that records what would have been sent."""

    name = "recording"

    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent: list[tuple[str, channels_module.OutboundReply]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.download_error: Exception | None = None

    def queue(self, message: channels_module.InboundMessage) -> None:
        self.messages.append(message)

    def verify(self, request):
        return True, "challenge-ok"

    def parse(self, request):
        messages, self.messages = self.messages, []
        return messages

    def download_media(self, message, dest_path):
        self.download_calls.append((message.media_id or message.media_url or "", dest_path))
        if self.download_error:
            raise self.download_error
        # Write a tiny file so the transcription path has something real.
        with open(dest_path, "wb") as handle:
            handle.write(b"OggS\x00fake-audio")
        return dest_path

    def send(self, to, reply):
        self.sent.append((to, reply))
        return True


@pytest.fixture
def channel(monkeypatch):
    recorder = RecordingChannel()
    monkeypatch.setattr(channels_module, "_channel_cache", recorder)
    return recorder


def inbound(text: str, message_id: str = "SM1", sender: str = SENDER, **kwargs):
    return channels_module.InboundMessage(
        message_id=message_id, sender=sender, text=text, **kwargs
    )


def post(client, channel, message) -> None:
    channel.queue(message)
    response = client.post("/webhook", json={"entry": []})
    assert response.status_code == 200, response.get_data(as_text=True)


def reply_text(channel) -> str:
    assert channel.sent, "no reply was sent"
    return channel.sent[-1][1].text


# ---------------------------------------------------------------------------
# Route basics
# ---------------------------------------------------------------------------
def test_health_reports_configuration_and_degradation(app_client):
    payload = app_client.get("/health").get_json()
    assert payload["service"] == "health-myth-bot"
    assert payload["status"] in {"ok", "degraded"}
    assert payload["retrieval"]["records"] > 0
    assert "boot_warnings" in payload
    assert "voice" in payload and "cards" in payload


def test_health_resource_is_always_200(app_client):
    assert app_client.get("/health").status_code == 200


def test_delivery_receipts_are_acknowledged_without_a_reply(app_client, channel):
    response = app_client.post("/webhook", json={"entry": []})
    assert response.status_code == 200
    assert channel.sent == []


def test_duplicate_delivery_is_ignored(app_client, channel, monkeypatch):
    monkeypatch.setattr(
        app_module.rag_engine, "get_myth_rebuttal", lambda **_: "A real answer."
    )
    post(app_client, channel, inbound("is the polio vaccine safe for my child", "SM-dup"))
    post(app_client, channel, inbound("is the polio vaccine safe for my child", "SM-dup"))

    assert len(channel.sent) == 1, "a provider retry must not answer twice"
    with database._get_conn() as conn:
        logs = conn.execute("SELECT COUNT(*) AS c FROM logs").fetchone()["c"]
    assert logs == 1


# ---------------------------------------------------------------------------
# Language selection and detection
# ---------------------------------------------------------------------------
def test_first_short_message_gets_the_language_menu(app_client, channel):
    post(app_client, channel, inbound("fever"))
    assert "1 – English" in reply_text(channel)
    assert database.get_user_language(SENDER) is None


def test_menu_digit_sets_the_language(app_client, channel):
    post(app_client, channel, inbound("3"))
    assert database.get_user_language(SENDER) == "kn"
    assert reply_text(channel) == language.LANG_CONFIRMATIONS["kn"]


def test_language_keyword_reopens_the_menu(app_client, channel):
    database.set_user_language(SENDER, "hi")
    post(app_client, channel, inbound("भाषा"))
    assert "1 – English" in reply_text(channel)
    assert "हिन्दी" in reply_text(channel)


def test_kannada_message_is_detected_and_stored(app_client, channel, monkeypatch):
    monkeypatch.setattr(
        app_module.rag_engine, "get_myth_rebuttal", lambda **_: "ಖಚಿತ ಉತ್ತರ."
    )
    post(app_client, channel, inbound("ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ"))
    assert database.get_user_language(SENDER) == "kn"


def test_romanised_message_is_detected_and_stored(app_client, channel, monkeypatch):
    monkeypatch.setattr(
        app_module.rag_engine, "get_myth_rebuttal", lambda **_: "confirmed answer"
    )
    post(app_client, channel, inbound("nange tumba jvara ide, enu madali"))
    assert database.get_user_language(SENDER) == "kn"


# ---------------------------------------------------------------------------
# Emergency path
# ---------------------------------------------------------------------------
def test_emergency_is_answered_in_the_stored_language(app_client, channel):
    database.set_user_language(SENDER, "kn")
    post(app_client, channel, inbound("ನನ್ನ ಅಪ್ಪನ ಎದೆಯಲ್ಲಿ ತುಂಬಾ ನೋವು ಇದೆ"))

    text = reply_text(channel)
    assert any(ord(c) in range(0x0C80, 0x0D00) for c in text), "emergency not translated"
    with database._get_conn() as conn:
        row = conn.execute(
            "SELECT category, is_emergency FROM logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["category"] == "Emergency"
    assert row["is_emergency"] == 1


def test_emergency_does_not_hit_the_answer_backend(app_client, channel, monkeypatch):
    def explode(**kwargs):
        raise AssertionError("emergency path must not call the LLM")

    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", explode)
    post(app_client, channel, inbound("chest pain"))
    assert "medical emergency" in reply_text(channel)


# ---------------------------------------------------------------------------
# Pending state must not swallow the next question
# ---------------------------------------------------------------------------
def test_fallback_prompts_to_flag_and_yes_flags_it(app_client, channel, monkeypatch):
    monkeypatch.setattr(
        app_module.rag_engine,
        "get_myth_rebuttal",
        lambda **_: language.FALLBACK_MESSAGES["en"],
    )
    post(app_client, channel, inbound("do copper bracelets cure arthritis"))
    assert database.get_pending_state(SENDER, "flag") is not None

    post(app_client, channel, inbound("yes", "SM2"))
    assert reply_text(channel) == language.FLAG_ACK["en"]
    assert database.get_pending_state(SENDER, "flag") is None
    assert len(database.fetch_flagged_myths("pending")) == 1


def test_a_new_question_after_a_prompt_is_answered_not_consumed(app_client, channel, monkeypatch):
    """The old code treated any reply as 'no' and answered 'Understood.'"""
    monkeypatch.setattr(
        app_module.rag_engine,
        "get_myth_rebuttal",
        lambda **_: language.FALLBACK_MESSAGES["en"],
    )
    post(app_client, channel, inbound("do copper bracelets cure arthritis"))

    monkeypatch.setattr(
        app_module.rag_engine,
        "get_myth_rebuttal",
        lambda **_: "Polio drops do not cause infertility.",
    )
    post(app_client, channel, inbound("is the polio vaccine safe for my child", "SM2"))

    text = reply_text(channel)
    assert "Polio drops" in text
    assert text != language.FLAG_DECLINED["en"]
    assert len(database.fetch_flagged_myths("pending")) == 0


def test_feedback_rating_is_recorded_from_plain_text(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))
    assert database.get_pending_state(SENDER, "feedback") is not None

    post(app_client, channel, inbound("👍", "SM2"))
    assert reply_text(channel) == language.FEEDBACK_ACK["en"]
    with database._get_conn() as conn:
        ratings = [r["rating"] for r in conn.execute("SELECT rating FROM feedback")]
    assert ratings == [1]


def test_button_taps_resolve_to_intents(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))

    post(app_client, channel, inbound("feedback_down", "SM2"))
    with database._get_conn() as conn:
        ratings = [r["rating"] for r in conn.execute("SELECT rating FROM feedback")]
    assert ratings == [-1]


def test_answers_carry_feedback_buttons(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))
    buttons = channel.sent[-1][1].buttons
    assert [button[0] for button in buttons] == ["feedback_up", "feedback_down"]


def test_answer_attaches_a_myth_card(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))
    image_path = channel.sent[-1][1].image_path
    assert image_path and os.path.exists(image_path)


# ---------------------------------------------------------------------------
# Kill switch and rate limit
# ---------------------------------------------------------------------------
def test_kill_switch_pauses_replies(app_client, channel, monkeypatch):
    def explode(**kwargs):
        raise AssertionError("a paused bot must not call the LLM")

    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", explode)
    database.set_bot_enabled(False)
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))
    assert reply_text(channel) == language.BOT_PAUSED_REPLY["en"]
    assert app_client.get("/health").get_json()["bot_enabled"] is False


def test_emergency_still_works_while_paused(app_client, channel):
    """Safety must not be behind the kill switch."""
    database.set_bot_enabled(False)
    post(app_client, channel, inbound("chest pain"))
    assert "medical emergency" in reply_text(channel)


def test_rate_limit_protects_the_free_tier(app_client, channel, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "3")
    assert app_module.rate_limit_per_hour() == 3
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")

    for index in range(4):
        post(app_client, channel, inbound(f"is the polio vaccine safe {index}", f"SM{index}"))

    assert reply_text(channel) == language.RATE_LIMITED_REPLY["en"]


def test_rate_limit_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "7")
    assert app_module.rate_limit_per_hour() == 7
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "not-a-number")
    assert app_module.rate_limit_per_hour() == app_module.DEFAULT_RATE_LIMIT_PER_HOUR


# ---------------------------------------------------------------------------
# Voice notes
# ---------------------------------------------------------------------------
def test_voice_note_is_transcribed_and_answered_by_voice(app_client, channel, monkeypatch):
    monkeypatch.setattr(safety_voice, "transcribe_audio", lambda path, lang="en": "polio vaccine safe")
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")

    message = inbound("", "SM-voice", kind="audio", media_id="media-1", media_type="audio/ogg")
    post(app_client, channel, message)

    assert channel.download_calls, "the voice note was never downloaded"
    assert "Answer." in reply_text(channel)


def test_failed_audio_download_leaves_no_temp_file(app_client, channel, monkeypatch, tmp_path):
    """The leak: the temp file was created before the download, so an early
    return skipped cleanup."""
    seen: list[str] = []
    original = channel.download_media

    def fail(message, dest_path):
        seen.append(dest_path)
        original(message, dest_path)
        raise RuntimeError("network died")

    monkeypatch.setattr(channel, "download_media", fail)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    message = inbound("", "SM-voice", kind="audio", media_id="media-1", media_type="audio/ogg")
    post(app_client, channel, message)

    assert reply_text(channel) == language.AUDIO_FAILED_REPLY["en"]
    for path in seen:
        assert not os.path.exists(path), f"leaked temp file {path}"


def test_transcription_failure_tells_the_user_to_type(app_client, channel, monkeypatch):
    monkeypatch.setattr(safety_voice, "transcribe_audio", lambda path, lang="en": "")
    message = inbound("", "SM-voice", kind="audio", media_id="media-1", media_type="audio/ogg")
    post(app_client, channel, message)
    assert reply_text(channel) == language.AUDIO_FAILED_REPLY["en"]


# ---------------------------------------------------------------------------
# WhatsApp Cloud API adapter
# ---------------------------------------------------------------------------
@pytest.fixture
def whatsapp_env(monkeypatch):
    """Select the Cloud API adapter with signature validation enforced."""
    monkeypatch.setitem(os.environ, "CHANNEL", "whatsapp")
    monkeypatch.setitem(os.environ, "VALIDATE_WEBHOOK_SIGNATURES", "true")
    monkeypatch.setitem(os.environ, "WHATSAPP_APP_SECRET", "test-app-secret")
    channels_module.reset_channel_cache()
    yield
    channels_module.reset_channel_cache()


def whatsapp_request(client, payload: dict, secret: str | None = None, signed: bool = True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signed and secret:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post("/webhook", data=body, headers=headers)


def test_whatsapp_signature_validation(app_client, monkeypatch):
    monkeypatch.setitem(os.environ, "VALIDATE_WEBHOOK_SIGNATURES", "true")
    channel = channels_module.WhatsAppCloudChannel()

    assert channel.verify_signature(b"{}", "sha256=deadbeef") is False
    assert channel.verify_signature(b"{}", "") is False

    digest = hmac.new(b"test-app-secret", b"{}", hashlib.sha256).hexdigest()
    assert channel.verify_signature(b"{}", f"sha256={digest}") is True


def test_whatsapp_webhook_rejects_an_unsigned_post(app_client, whatsapp_env):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": SENDER, "id": "wamid.1", "type": "text", "text": {"body": "hi"}}
    ]}}]}]}
    assert whatsapp_request(app_client, payload, signed=False).status_code == 403


def test_whatsapp_webhook_rejects_a_tampered_body(app_client, whatsapp_env):
    """A valid signature over a *different* body must not be accepted."""
    body = json.dumps({"entry": []}).encode()
    digest = hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    response = app_client.post(
        "/webhook",
        data=json.dumps({"entry": [{"changes": []}]}).encode(),
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"},
    )
    assert response.status_code == 403


def test_whatsapp_webhook_rejects_everything_when_the_secret_is_missing(app_client, monkeypatch):
    """Fail closed: no secret means the request cannot be authenticated."""
    monkeypatch.setitem(os.environ, "CHANNEL", "whatsapp")
    monkeypatch.setitem(os.environ, "VALIDATE_WEBHOOK_SIGNATURES", "true")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    channels_module.reset_channel_cache()
    payload = {"entry": []}
    assert whatsapp_request(app_client, payload, secret="anything").status_code == 403


def test_whatsapp_webhook_accepts_a_correctly_signed_post(app_client, whatsapp_env):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": SENDER, "id": "wamid.2", "type": "text", "text": {"body": "chest pain"}}
    ]}}]}]}
    response = whatsapp_request(app_client, payload, secret="test-app-secret")
    assert response.status_code == 200, response.get_data(as_text=True)

    # And the message really was handled: the emergency hit the logs table.
    with database._get_conn() as conn:
        row = conn.execute(
            "SELECT category FROM logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["category"] == "Emergency"


def test_whatsapp_verification_handshake():
    channel = channels_module.WhatsAppCloudChannel()

    class Request:
        method = "GET"
        args = {"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                "hub.challenge": "1234"}

    authorised, challenge = channel.verify(Request())
    assert authorised and challenge == "1234"

    class BadRequest(Request):
        args = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1234"}

    assert channel.verify(BadRequest()) == (False, None)


def test_whatsapp_parses_text_and_voice_messages():
    channel = channels_module.WhatsAppCloudChannel()
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "919999000001", "id": "m1", "type": "text",
             "text": {"body": "  hello  "}},
            {"from": "919999000001", "id": "m2", "type": "audio",
             "audio": {"id": "media-9", "mime_type": "audio/ogg; codecs=opus"}},
            {"from": "919999000001", "id": "m3", "type": "interactive",
             "interactive": {"button_reply": {"id": "feedback_up", "title": "👍 Helpful"}}},
        ]}}]}]
    }

    class Request:
        def get_json(self, silent=False):
            return payload

    messages = channel.parse(Request())
    assert [m.kind for m in messages] == ["text", "audio", "text"]
    assert messages[0].text == "hello"
    assert messages[1].has_audio and messages[1].media_id == "media-9"
    assert messages[2].text == "feedback_up"


def test_whatsapp_text_payload_shape():
    channel = channels_module.WhatsAppCloudChannel()
    payloads = channel._build_payloads("9199", channels_module.OutboundReply(text="hi")),
    payload = payloads[0][0]
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "hi"


def test_whatsapp_interactive_payload_respects_limits():
    channel = channels_module.WhatsAppCloudChannel()
    reply = channels_module.OutboundReply(
        text="body",
        buttons=[("id-1", "A very long button title indeed"), ("id-2", "b"), ("id-3", "c"), ("id-4", "d")],
    )
    payload = channel._build_payloads("9199", reply)[0]
    buttons = payload["interactive"]["action"]["buttons"]
    assert payload["type"] == "interactive"
    assert len(buttons) == channels_module.MAX_BUTTONS
    assert len(buttons[0]["reply"]["title"]) <= channels_module.MAX_BUTTON_TITLE_CHARS


def test_reply_is_truncated_rather_than_rejected():
    reply = channels_module.OutboundReply(text="x" * (channels_module.MAX_TEXT_CHARS + 50))
    assert len(reply.clamped_text()) <= channels_module.MAX_TEXT_CHARS


def test_audio_suffix_detection():
    assert channels_module.audio_suffix_for("audio/ogg; codecs=opus") == ".ogg"
    assert channels_module.audio_suffix_for("audio/mpeg") == ".mp3"
    assert channels_module.audio_suffix_for(None) == ".ogg"


# ---------------------------------------------------------------------------
# Advisory window (the free-tier guarantee)
# ---------------------------------------------------------------------------
def test_advisory_only_reaches_users_inside_their_window(monkeypatch):
    from bot import advisory

    inside = "whatsapp:+919999000010"
    outside = "whatsapp:+919999000011"
    database.open_service_window(inside)
    database.set_user_language(inside, "kn")

    sent: list[str] = []

    class Recorder:
        def send(self, to, reply):
            sent.append(to)
            return True

    # Simulate a window that closed between listing and sending.
    real_open = database.service_window_open
    monkeypatch.setattr(
        database,
        "service_window_open",
        lambda phone, hours=24.0: False if phone == inside else real_open(phone, hours),
    )
    result = advisory.dispatch("Vaccines", channel=Recorder(), force=True)

    # Nobody inside a window → nothing sent, and the skip is counted, not silent.
    assert sent == []
    assert result["sent"] == 0
    record = database.fetch_recent_advisories()[0]
    assert record["sent_count"] == 0
    assert record["skipped_count"] == 1


def test_advisory_sends_to_a_user_inside_their_window(monkeypatch):
    from bot import advisory

    inside = "whatsapp:+919999000012"
    database.open_service_window(inside)
    database.set_user_language(inside, "kn")

    sent: list[tuple[str, str]] = []

    class Recorder:
        def send(self, to, reply):
            sent.append((to, reply.text))
            return True

    result = advisory.dispatch("Vaccines", channel=Recorder(), force=True)
    assert result["sent"] == 1
    assert sent[0][0] == inside
    assert any(ord(c) in range(0x0C80, 0x0D00) for c in sent[0][1])


def test_advisory_is_suppressed_by_the_kill_switch():
    from bot import advisory

    database.set_bot_enabled(False)
    result = advisory.dispatch("Vaccines", force=True)
    assert result["sent"] == 0
    assert "kill switch" in result["reason"]


def test_advisory_cooldown_prevents_repeat_messages():
    from bot import advisory

    phone = "whatsapp:+919999000013"
    database.open_service_window(phone)
    database.log_advisory("Vaccines", "body", 1, 0)

    assert advisory.cooldown_active("Vaccines") is True
    result = advisory.dispatch("Vaccines")
    assert result["sent"] == 0 and result["cooldown"] is True


def test_advisory_copy_exists_in_every_language():
    from bot import advisory

    for code in language.SUPPORTED_LANGUAGES:
        body = advisory.build_body("Vaccines", code)
        assert "Vaccines" in body


# ---------------------------------------------------------------------------
# Review loop
# ---------------------------------------------------------------------------
def test_flagged_myth_review_round_trip():
    database.flag_myth("cow urine cures everything", "kn")
    flags = database.fetch_flagged_myths("pending")
    assert len(flags) == 1

    database.resolve_flagged_myth(flags[0]["id"], "approved", "checked with DHO")
    resolved = database.fetch_flagged_myths("approved")
    assert resolved[0]["review_note"] == "checked with DHO"
    assert database.count_flagged_myths("pending") == 0


def test_invalid_review_status_is_rejected():
    database.flag_myth("x", "en")
    flag_id = database.fetch_flagged_myths("pending")[0]["id"]
    with pytest.raises(ValueError):
        database.resolve_flagged_myth(flag_id, "maybe")


def test_summary_stats_include_the_new_operational_fields():
    stats = database.fetch_summary_stats()
    assert "pending_reviews" in stats
    assert "users_in_window" in stats


# ---------------------------------------------------------------------------
# Retrieval integration through the app
# ---------------------------------------------------------------------------
def test_category_logged_comes_from_retrieval(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("is the polio vaccine safe for my child"))
    with database._get_conn() as conn:
        row = conn.execute("SELECT category FROM logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["category"] == "Vaccines"


def test_no_card_for_a_question_the_corpus_does_not_cover(app_client, channel, monkeypatch):
    monkeypatch.setattr(app_module.rag_engine, "get_myth_rebuttal", lambda **_: "Answer.")
    post(app_client, channel, inbound("why does my online order say in transit"))
    assert channel.sent[-1][1].image_path is None
    assert retrieval.get_retriever().category_for("why does my online order say in transit") == "General"
