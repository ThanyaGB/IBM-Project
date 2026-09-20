"""Messaging channel adapters.

Two channels, one interface:

* ``whatsapp`` — the official WhatsApp Cloud API (Graph API).  Primary.
* ``twilio``   — the legacy Twilio WhatsApp adapter, kept behind
  ``CHANNEL=twilio`` so existing deployments keep working.

Why Cloud API is the free path
------------------------------
Inbound messages are never charged, and a reply that is not a *template*
(text, image, audio, interactive buttons) is free while the 24-hour customer
service window the user's own message opened is still open.  Only template
messages sent outside that window cost money, and this bot never sends them:
every send here is a reply to an inbound message.  So the architecture itself
is the cost control — see ``advisory.py`` for the window check that keeps it
that way.

Signature validation
--------------------
Both adapters verify the request before parsing it:

* WhatsApp: ``X-Hub-Signature-256`` = HMAC-SHA256 of the raw body, keyed with
  the Meta app secret.  Comparison is constant-time.
* Twilio: the SDK's ``RequestValidator`` over the full public URL plus form
  fields.

Both fail *closed* when their secret is missing, because an unauthenticated
webhook lets anyone who learns the URL inject messages into the bot.  The
check is controlled by ``VALIDATE_WEBHOOK_SIGNATURES`` (default on); the test
suite turns it off for the cases that are not about signatures, and separate
tests cover the signature paths themselves.

Media
-----
Outbound media is uploaded to the provider and referenced by id, so serving
card images no longer needs a public URL or a tunnel.  That also removes the
old ``CARD_BASE_URL`` setting, which pointed at ``/cards`` while Flask served
``/static/cards`` — a mismatch that silently dropped every card.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import requests
from flask import Request

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# WhatsApp's own limits.  Exceeding them makes the send fail, so we truncate
# rather than lose the whole reply.
MAX_TEXT_CHARS = 4000
MAX_BUTTONS = 3
MAX_BUTTON_TITLE_CHARS = 20

# Accepted inbound audio types, mapped to the file suffix we store them with.
_AUDIO_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ogg", ".ogg"),
    ("opus", ".ogg"),
    ("mpeg", ".mp3"),
    ("mp3", ".mp3"),
    ("mp4", ".mp4"),
    ("m4a", ".m4a"),
    ("aac", ".aac"),
    ("amr", ".amr"),
    ("wav", ".wav"),
)

REQUEST_TIMEOUT = 30


@dataclass
class InboundMessage:
    """A provider message, normalised into what the bot core needs."""

    message_id: str
    sender: str
    kind: str = "text"  # text | audio | image | other
    text: str = ""
    media_id: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_audio(self) -> bool:
        return self.kind == "audio" and bool(self.media_id or self.media_url)


@dataclass
class OutboundReply:
    """What the bot wants to send back.

    Only one media attachment is possible per send, so a reply is text plus
    optionally one image (the myth card) or one audio clip (the spoken
    answer) — image wins when both are present, because the card carries the
    verified wording and the text reply already carries the answer.
    """

    text: str
    image_path: str | None = None
    audio_path: str | None = None
    buttons: list[tuple[str, str]] = field(default_factory=list)

    def clamped_text(self) -> str:
        if len(self.text) <= MAX_TEXT_CHARS:
            return self.text
        logger.warning(
            "Reply truncated from %d to %d chars", len(self.text), MAX_TEXT_CHARS
        )
        return self.text[: MAX_TEXT_CHARS - 1] + "…"


class Channel(Protocol):
    """Interface every adapter implements."""

    name: str

    def verify(self, request: Request) -> tuple[bool, str | None]:
        """Return (authorised, plain-text challenge response)."""

    def parse(self, request: Request) -> list[InboundMessage]:
        """Normalise an inbound webhook request into messages."""

    def download_media(self, message: InboundMessage, dest_path: str) -> str:
        """Fetch inbound media to `dest_path`."""

    def send(self, to: str, reply: OutboundReply) -> bool:
        """Deliver `reply` to `to`.  Returns True on success."""


def validation_enabled() -> bool:
    """Whether inbound webhooks must carry a valid signature."""
    return os.environ.get("VALIDATE_WEBHOOK_SIGNATURES", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _audio_suffix(content_type: str | None) -> str:
    lowered = (content_type or "").lower()
    for needle, suffix in _AUDIO_SUFFIXES:
        if needle in lowered:
            return suffix
    return ".ogg"


# ---------------------------------------------------------------------------
# WhatsApp Cloud API
# ---------------------------------------------------------------------------
class WhatsAppCloudChannel:
    """Official Meta WhatsApp Cloud API adapter."""

    name = "whatsapp"

    def __init__(
        self,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        app_secret: str | None = None,
        verify_token: str | None = None,
    ) -> None:
        self.access_token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self.phone_number_id = phone_number_id or os.environ.get(
            "WHATSAPP_PHONE_NUMBER_ID", ""
        )
        self.app_secret = app_secret or os.environ.get("WHATSAPP_APP_SECRET", "")
        self.verify_token = verify_token or os.environ.get(
            "WHATSAPP_VERIFY_TOKEN", ""
        )

    # -- auth ---------------------------------------------------------------
    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def verify(self, request: Request) -> tuple[bool, str | None]:
        """Handle the GET subscribe handshake and validate POST signatures."""
        if request.method == "GET":
            mode = request.args.get("hub.mode")
            token = request.args.get("hub.verify_token")
            challenge = request.args.get("hub.challenge", "")
            if mode == "subscribe" and self.verify_token and token == self.verify_token:
                logger.info("WhatsApp webhook verification handshake accepted")
                return True, challenge
            logger.warning(
                "WhatsApp webhook verification rejected (mode=%r, token_match=%s)",
                mode,
                token == self.verify_token,
            )
            return False, None

        if not validation_enabled():
            logger.warning(
                "VALIDATE_WEBHOOK_SIGNATURES is off — accepting an unverified "
                "WhatsApp webhook. Never run this way outside tests."
            )
            return True, None

        if not self.app_secret:
            # Fail closed: an unsigned webhook is an unauthenticated webhook,
            # and anyone who learns the URL could inject messages.
            logger.error(
                "WHATSAPP_APP_SECRET is not set — rejecting webhook. "
                "Set it to the Meta app secret to enable signature validation."
            )
            return False, None

        if not self.verify_signature(
            request.get_data(), request.headers.get("X-Hub-Signature-256", "")
        ):
            logger.warning("WhatsApp webhook signature mismatch — rejecting")
            return False, None
        return True, None

    # -- inbound ------------------------------------------------------------
    def parse(self, request: Request) -> list[InboundMessage]:
        try:
            payload = request.get_json(silent=True) or {}
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("WhatsApp payload was not JSON: %s", exc)
            return []

        messages: list[InboundMessage] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for message in value.get("messages") or []:
                    parsed = self._parse_message(message)
                    if parsed is not None:
                        messages.append(parsed)
        return messages

    def _parse_message(self, message: Mapping[str, Any]) -> InboundMessage | None:
        sender = str(message.get("from") or "")
        if not sender:
            return None

        kind = message.get("type") or "other"
        text = ""
        media_id = None
        media_type = None

        if kind == "text":
            text = ((message.get("text") or {}).get("body") or "").strip()
        elif kind == "audio":
            audio = message.get("audio") or {}
            media_id = audio.get("id")
            media_type = audio.get("mime_type") or "audio/ogg"
        elif kind in {"voice", "document"}:
            media = message.get(kind) or {}
            media_id = media.get("id")
            media_type = media.get("mime_type")
            if media_type and media_type.startswith("audio"):
                kind = "audio"
        elif kind == "image":
            image = message.get("image") or {}
            media_id = image.get("id")
            media_type = image.get("mime_type")
        elif kind == "button":
            text = ((message.get("button") or {}).get("text") or "").strip()
            kind = "text"
        elif kind == "interactive":
            interactive = message.get("interactive") or {}
            chosen = interactive.get("button_reply") or interactive.get(
                "list_reply"
            ) or {}
            text = (chosen.get("id") or chosen.get("title") or "").strip()
            kind = "text"

        return InboundMessage(
            message_id=str(message.get("id") or ""),
            sender=sender,
            kind=kind,
            text=text,
            media_id=media_id,
            media_type=media_type,
            raw=dict(message),
        )

    def download_media(self, message: InboundMessage, dest_path: str) -> str:
        """Two-step download: resolve the media id, then fetch the bytes."""
        media_ref = message.media_id or message.media_url
        if not media_ref:
            raise ValueError("inbound message has no media reference")

        url = media_ref
        if message.media_id and not message.media_url:
            meta = requests.get(
                f"{GRAPH_API_BASE}/{media_ref}",
                headers=self._bearer(),
                timeout=REQUEST_TIMEOUT,
            )
            meta.raise_for_status()
            body = meta.json()
            url = body.get("url")
            if not url:
                raise ValueError("media metadata response contained no url")

        response = requests.get(
            url, headers=self._bearer(), timeout=REQUEST_TIMEOUT, stream=True
        )
        response.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=8192):
                fh.write(chunk)
        logger.info("Downloaded inbound media to %s", dest_path)
        return dest_path

    # -- outbound -----------------------------------------------------------
    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Constant-time check of an ``X-Hub-Signature-256`` header.

        Separated from ``verify`` so it can be unit-tested without a Flask
        request object.
        """
        if not self.app_secret:
            return False
        expected = hmac.new(
            self.app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        supplied = (signature or "").removeprefix("sha256=").strip()
        return bool(supplied) and hmac.compare_digest(expected, supplied)

    def upload_media(self, media_path: str) -> str | None:
        """Upload a local file, returning its reusable media id."""
        if not os.path.exists(media_path):
            logger.warning("upload_media: %s does not exist", media_path)
            return None

        mime_type, _ = mimetypes.guess_type(media_path)
        mime_type = mime_type or "application/octet-stream"
        # WhatsApp only accepts audio/ogg with the Opus codec, and rejects the
        # plain "audio/ogg" that mimetypes reports for .ogg.
        if media_path.endswith(".ogg"):
            mime_type = "audio/ogg; codecs=opus"

        try:
            with open(media_path, "rb") as fh:
                response = requests.post(
                    f"{GRAPH_API_BASE}/{self.phone_number_id}/media",
                    headers=self._bearer(),
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files={"file": (os.path.basename(media_path), fh, mime_type)},
                    timeout=REQUEST_TIMEOUT * 2,
                )
            if response.status_code >= 400:
                logger.error(
                    "WhatsApp media upload failed (%d): %s",
                    response.status_code,
                    response.text[:300],
                )
                return None
            media_id = (response.json() or {}).get("id")
            logger.info("Uploaded %s as media id %s", media_path, media_id)
            return media_id
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("WhatsApp media upload raised: %s", exc)
            return None

    def send(self, to: str, reply: OutboundReply) -> bool:
        if not self.access_token or not self.phone_number_id:
            logger.error(
                "WhatsApp send skipped: WHATSAPP_ACCESS_TOKEN or "
                "WHATSAPP_PHONE_NUMBER_ID is not set"
            )
            return False

        sent_any = False
        for payload in self._build_payloads(to, reply):
            if self._post_message(payload):
                sent_any = True
        return sent_any

    def _build_payloads(self, to: str, reply: OutboundReply) -> list[dict[str, Any]]:
        """Reply payloads, in order. Media caption rides with the media."""
        payloads: list[dict[str, Any]] = []
        caption = reply.clamped_text()

        if reply.image_path:
            media_id = self.upload_media(reply.image_path)
            if media_id:
                payloads.append(
                    {
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": to,
                        "type": "image",
                        "image": {"id": media_id, "caption": caption},
                    }
                )
                return payloads
            logger.warning("Image upload failed — falling back to text reply")

        if reply.audio_path:
            media_id = self.upload_media(reply.audio_path)
            if media_id:
                # The spoken answer is the reply; the text is what was spoken.
                payloads.append(
                    {
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": to,
                        "type": "audio",
                        "audio": {"id": media_id},
                    }
                )
                payloads.append(
                    {
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": to,
                        "type": "text",
                        "text": {"body": caption[:1024], "preview_url": False},
                    }
                )
                return payloads
            logger.warning("Audio upload failed — falling back to text reply")

        if reply.buttons and _supports_interactive():
            payloads.append(self._interactive_payload(to, caption, reply.buttons))
            return payloads

        payloads.append(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"body": caption, "preview_url": False},
            }
        )
        return payloads

    def _interactive_payload(
        self, to: str, body: str, buttons: Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        trimmed = list(buttons)[:MAX_BUTTONS]
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": button_id[:256],
                                "title": title[:MAX_BUTTON_TITLE_CHARS],
                            },
                        }
                        for button_id, title in trimmed
                    ]
                },
            },
        }

    def _post_message(self, payload: dict[str, Any]) -> bool:
        try:
            response = requests.post(
                f"{GRAPH_API_BASE}/{self.phone_number_id}/messages",
                headers={**self._bearer(), "Content-Type": "application/json"},
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("WhatsApp send raised: %s", exc)
            return False

        if response.status_code >= 400:
            logger.error(
                "WhatsApp send failed (%d): %s",
                response.status_code,
                response.text[:300],
            )
            return False
        logger.info("WhatsApp message sent (type=%s)", payload.get("type"))
        return True


def _supports_interactive() -> bool:
    """Whether interactive button messages may be sent.

    They work on a current Cloud API version; the flag exists so a deployment
    pinned to an older API can fall back to plain text without a code change.
    """
    return os.environ.get("SUPPORTS_INTERACTIVE_MESSAGES", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ---------------------------------------------------------------------------
# Twilio (legacy)
# ---------------------------------------------------------------------------
class TwilioChannel:
    """Legacy Twilio WhatsApp adapter, kept for existing deployments."""

    name = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")

    def verify(self, request: Request) -> tuple[bool, str | None]:
        if request.method == "GET":
            return True, request.args.get("hub.challenge", "")

        if not validation_enabled():
            logger.warning(
                "VALIDATE_WEBHOOK_SIGNATURES is off — accepting an unverified "
                "Twilio webhook. Never run this way outside tests."
            )
            return True, None

        if not self.auth_token:
            logger.error(
                "TWILIO_AUTH_TOKEN is not set — rejecting webhook. Without it "
                "the request cannot be authenticated and anyone who knows this "
                "URL could post to it."
            )
            return False, None

        try:
            from twilio.request_validator import (  # noqa: PLC0415
                RequestValidator,
            )
        except ImportError:
            logger.error(
                "twilio is not installed — rejecting webhook rather than "
                "accepting an unverifiable request"
            )
            return False, None

        signature = request.headers.get("X-Twilio-Signature", "")
        validator = RequestValidator(self.auth_token)
        url = request.url
        # Twilio signs the *public* URL.  Behind a tunnel/proxy the app only
        # sees the internal one, so trust the forwarded host when present.
        forwarded_host = request.headers.get("X-Forwarded-Host")
        if forwarded_host:
            scheme = request.headers.get("X-Forwarded-Proto", "https")
            url = f"{scheme}://{forwarded_host}{request.path}"

        if not validator.validate(url, request.form.to_dict(), signature):
            logger.warning("Twilio signature mismatch — rejecting")
            return False, None
        return True, None

    def parse(self, request: Request) -> list[InboundMessage]:
        message_id = (
            request.form.get("MessageSid")
            or request.form.get("SmsMessageSid")
            or request.form.get("message_id")
            or ""
        )
        sender = request.form.get("From", "")
        if not sender:
            return []

        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0")
        body = (request.form.get("Body") or "").strip()

        kind = "text"
        if media_url and media_type and "audio" in media_type:
            kind = "audio"

        return [
            InboundMessage(
                message_id=message_id,
                sender=sender,
                kind=kind,
                text=body,
                media_url=media_url,
                media_type=media_type,
                raw=request.form.to_dict(),
            )
        ]

    def download_media(self, message: InboundMessage, dest_path: str) -> str:
        if not message.media_url:
            raise ValueError("inbound message has no media url")
        from bot import safety_voice  # noqa: PLC0415

        return safety_voice.download_audio(
            media_url=message.media_url,
            dest_path=dest_path,
            auth=(self.account_sid, self.auth_token),
        )

    def send(self, to: str, reply: OutboundReply) -> bool:
        """Send via the REST API (not TwiML) so replies work off-band too."""
        if not self.account_sid or not self.auth_token:
            logger.error("Twilio send skipped: credentials not configured")
            return False

        from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.account_sid}/Messages.json"
        )
        media_url = None
        if reply.image_path:
            base = os.environ.get("CARD_BASE_URL", "").rstrip("/")
            if base:
                media_url = f"{base}/{os.path.basename(reply.image_path)}"

        data: dict[str, Any] = {"From": from_number, "To": to, "Body": reply.clamped_text()}
        if media_url:
            data["MediaUrl"] = media_url

        try:
            response = requests.post(
                url,
                data=data,
                auth=(self.account_sid, self.auth_token),
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Twilio send raised: %s", exc)
            return False

        if response.status_code >= 400:
            logger.error(
                "Twilio send failed (%d): %s", response.status_code, response.text[:300]
            )
            return False
        logger.info("Twilio message sent")
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_CHANNELS = {"whatsapp": WhatsAppCloudChannel, "twilio": TwilioChannel}

_channel_cache: Channel | None = None


def get_channel(name: str | None = None, *, use_cache: bool = True) -> Channel:
    """Return the configured channel adapter."""
    global _channel_cache  # pylint: disable=global-statement
    if use_cache and _channel_cache is not None and name is None:
        return _channel_cache

    resolved = (name or os.environ.get("CHANNEL", "whatsapp")).strip().lower()
    if resolved in {"cloud", "meta", "whatsapp_cloud"}:
        resolved = "whatsapp"
    if resolved not in _CHANNELS:
        logger.warning("Unknown CHANNEL=%r — using 'whatsapp'", resolved)
        resolved = "whatsapp"

    channel = _CHANNELS[resolved]()
    logger.info("Using messaging channel: %s", channel.name)
    if use_cache and name is None:
        _channel_cache = channel
    return channel


def reset_channel_cache() -> None:
    """Drop the memoised adapter (used by tests)."""
    global _channel_cache  # pylint: disable=global-statement
    _channel_cache = None


def audio_suffix_for(content_type: str | None) -> str:
    return _audio_suffix(content_type)
