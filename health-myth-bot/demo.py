"""Website demo — landing page, phone-emulator endpoint, dashboard link.

Three routes, all channel-agnostic:

``GET  /``
    Serves the landing page (``static/index.html``).  Static assets — including
    the myth-card PNGs the demo embeds — are already served by Flask's default
    ``/static`` route, so no extra wiring is needed.

``GET /demo/config``
    Small JSON the static page fetches so it can link to the Streamlit
    dashboard without becoming a template.  Override with ``DASHBOARD_URL``.

``POST /demo/message``
    The emulator's brain.  Builds a synthetic inbound message from a
    per-session sender (``demo:<8 hex chars>``, generated in the browser and
    kept in ``sessionStorage``) and drives the *real* ``_handle_inbound``
    pipeline — retrieval, generation, cards, feedback, language detection,
    emergency short-circuit, rate limiting.  Nothing about the answering path
    is simulated.

Because it is a public, unauthenticated endpoint:

* Input is capped at 300 chars and stripped; sessions are rate-capped at
  ``DEMO_RATE_LIMIT_PER_HOUR`` (default 30, independent of the WhatsApp limit).
* ``log_to_dashboard`` (default true) is the visitor's visible toggle.  When it
  is false the two ``database.log_query`` calls are skipped, so dashboard
  analytics stay untouched while the demo still answers for real.  Feedback
  and myth reports from the emulator still write, on purpose — only a real
  person tapping a button can produce them, so they are signal, not noise.
* Failures degrade the way the bot does: any error in the pipeline returns the
  normal localised fallback reply, never a raw 500 or a traceback.
"""

from __future__ import annotations

import logging
import os
import re

from flask import Blueprint, jsonify, request, send_from_directory

from bot import channels as channels_module
from bot import database
from bot.language import ERROR_REPLY, get_string, normalise_language

logger = logging.getLogger(__name__)

DEMO_INPUT_LIMIT = 300
DEMO_RATE_LIMIT_PER_HOUR = int(os.environ.get("DEMO_RATE_LIMIT_PER_HOUR", "30"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8501")

# Browser-supplied session ids: 8-64 hex chars.  Anything else is discarded so
# a caller cannot smuggle odd strings into the sender / logs.
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")

demo_bp = Blueprint("demo", __name__)


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------
@demo_bp.route("/", methods=["GET"])
def landing():
    """Serve the landing page."""
    return send_from_directory("static", "index.html")


@demo_bp.route("/demo", methods=["GET"])
def demo_page():
    """Standalone live-demo screen: phone emulator + dashboard mirror toggle."""
    return send_from_directory("static", "demo.html")


# ---------------------------------------------------------------------------
# Dashboard link config
# ---------------------------------------------------------------------------
@demo_bp.route("/demo/config", methods=["GET"])
def config():
    """Dashboard URL for the static page's link (no templating needed)."""
    return jsonify({"dashboard_url": DASHBOARD_URL})


# ---------------------------------------------------------------------------
# Emulator endpoint
# ---------------------------------------------------------------------------
@demo_bp.route("/demo/message", methods=["POST"])
def message():
    """Run one demo turn through the real bot core.

    Request JSON: ``{ message, session_id, log_to_dashboard? }``.

    ``session_id`` is browser-generated (8-64 hex chars) and becomes the
    synthetic ``demo:<id>`` sender, so pending state (feedback prompts, myth
    reports) and the per-user rate limit are per visitor session — exactly as
    they would be per phone number on WhatsApp.
    """
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("message") or "").strip()
    session_id = str(payload.get("session_id") or "").strip().lower()

    if not text:
        return jsonify({"error": "empty message"}), 400
    if len(text) > DEMO_INPUT_LIMIT:
        return jsonify(
            {"error": f"message too long (max {DEMO_INPUT_LIMIT} chars)"}
        ), 400
    if not _SESSION_ID_RE.match(session_id):
        random_hex = os.urandom(8).hex()
        session_id = random_hex
        # 400 rather than silently substituting: a session id that is not hex
        # means a caller we did not write this endpoint for is talking to us.
        return (
            jsonify(
                {
                    "error": "invalid session_id (expected 8-64 hex chars)",
                    "session_id": random_hex,
                }
            ),
            400,
        )

    sender = f"_demo_prefix(){session_id}"

    # Demo sessions are capped independently of the WhatsApp rate limit so a
    # visitor cannot exhaust the free-tier budget from the website.
    if database.count_recent_queries(sender, hours=1) >= DEMO_RATE_LIMIT_PER_HOUR:
        return jsonify(
            {
                "reply_text": "You've sent quite a few questions in a short time — "
                "let's pause here so the service stays available for everyone. 🙏",
                "buttons": [],
                "rate_limited": True,
            }
        ), 200

    message = channels_module.InboundMessage(
        message_id=f"demo-{session_id}-{os.urandom(4).hex()}",
        sender=sender,
        kind="text",
        text=text,
        raw={"demo": True, "log_to_dashboard": bool(payload.get("log_to_dashboard", True))},
    )

    channel = channels_module.get_channel()
    try:
        # Imported here rather than at module level: app.py registers this
        # blueprint at its own import time, so a module-level `import app`
        # here would be circular when anything imports `demo` first.
        import app as app_module  # noqa: PLC0415

        reply = app_module._handle_inbound(
            message, channel, log_queries=bool(payload.get("log_to_dashboard", True))
        )
    except Exception as exc:  # pylint: disable=broad-except
        # Degrade exactly like the webhook path does: localised fallback reply,
        # never a raw 500 or a traceback reaching the browser.
        logger.exception("Demo pipeline failed: %s", exc)
        reply = channels_module.OutboundReply(
            text=get_string(ERROR_REPLY, "en"), buttons=[]
        )

    if reply is None:
        reply = channels_module.OutboundReply(text="", buttons=[])

    image_url = None
    if reply.image_path:
        filename = os.path.basename(reply.image_path)
        image_url = f"/static/cards/{filename}"

    return jsonify(
        {
            "reply_text": reply.clamped_text(),
            "image_url": image_url,
            "buttons": [{"id": bid, "title": title} for bid, title in reply.buttons],
            "rate_limited": False,
        }
    )


def _demo_prefix() -> str:
    """The demo sender prefix reserved by the bot core (app.DEMO_SENDER_PREFIX)."""
    import app as app_module  # noqa: PLC0415

    return app_module.DEMO_SENDER_PREFIX


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
def registerDemoBlueprint(flask_app) -> None:
    """Register the blueprint on the given Flask app (called from app.py)."""
    flask_app.register_blueprint(demo_bp)
