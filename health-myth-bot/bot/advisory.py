"""Proactive rumour-spike advisories — sent only where sending is free.

The constraint
--------------
WhatsApp charges for *template* messages sent outside the 24-hour customer
service window a user's own message opens.  Inbound messages are never charged,
and non-template replies inside the window are free.  So a proactive message is
free only to someone who has messaged us in the last 24 hours — and the moment
we send to anyone else, the bot acquires a per-message bill.

This module therefore does not "try" to stay free, it structurally cannot spend
money: `dispatch` iterates the window table and skips anyone outside it, and the
skip count is recorded so the trade-off is visible instead of invisible.

Advisory copy is hand-written UI text in every supported language, like the
rest of ``language.py``.  It deliberately does not go through the LLM: an
unsolicited health message is the last place to introduce wording that no human
has read.

Nothing here runs from the request path — call it from the dashboard or the CLI
after ``alerting.detect_rumor_spike`` reports a spike.
"""

from __future__ import annotations

import logging
import os

from bot import channels as channels_module
from bot import database
from bot.language import SUPPORTED_LANGUAGES, normalise_language

logger = logging.getLogger(__name__)

# Spikes are re-detected every time the dashboard loads; never re-message the
# same category inside this window.
COOLDOWN_HOURS = float(os.environ.get("ADVISORY_COOLDOWN_HOURS", "12"))

# Sent only when the bot is enabled and only to users inside their window.
ADVISORY_TEMPLATE: dict[str, str] = {
    "en": (
        "📣 We're seeing many questions about *{category}* in your area lately.\n\n"
        "Reply with any question about it and I'll send the verified facts and a "
        "card you can share with family and neighbours."
    ),
    "hi": (
        "📣 आपके इलाके में अभी *{category}* के बारे में कई सवाल पूछे जा रहे हैं।\n\n"
        "इस विषय पर कोई भी सवाल भेजें — मैं सत्यापित जानकारी और परिवार तथा "
        "पड़ोसियों के साथ साझा करने के लिए एक कार्ड भेजूँगा।"
    ),
    "kn": (
        "📣 ಇತ್ತೀಚೆಗೆ ನಿಮ್ಮ ಪ್ರದೇಶದಲ್ಲಿ *{category}* ಬಗ್ಗೆ ಹಲವು ಪ್ರಶ್ನೆಗಳು "
        "ಬರುತ್ತಿವೆ.\n\n"
        "ಇದರ ಬಗ್ಗೆ ಯಾವುದೇ ಪ್ರಶ್ನೆ ಕಳುಹಿಸಿ — ನಾನು ಪರಿಶೀಲಿಸಿದ ಮಾಹಿತಿ ಮತ್ತು "
        "ಕುಟುಂಬ ಹಾಗೂ ನೆರೆಹೊರೆಯವರೊಂದಿಗೆ ಹಂಚಿಕೊಳ್ಳಲು ಕಾರ್ಡ್ ಕಳುಹಿಸುತ್ತೇನೆ."
    ),
}


def build_body(category: str, language: str = "en") -> str:
    """Advisory text for one category and language."""
    template = ADVISORY_TEMPLATE.get(
        normalise_language(language), ADVISORY_TEMPLATE["en"]
    )
    return template.format(category=category)


def cooldown_active(category: str, window_hours: float = COOLDOWN_HOURS) -> bool:
    """True when this category was already advised recently."""
    return database.within_hours(database.last_advisory_at(category), window_hours)


def dispatch(
    category: str,
    *,
    body: str | None = None,
    dry_run: bool = False,
    channel: channels_module.Channel | None = None,
    force: bool = False,
    limit: int = 500,
) -> dict:
    """Send an advisory about `category` to everyone inside their reply window.

    Returns a summary dict: ``sent``, ``skipped``, ``reason``, ``cooldown``.
    """
    if not database.bot_enabled():
        reason = "kill switch is on (bot disabled)"
        logger.warning("Advisory for %s suppressed: %s", category, reason)
        return {"sent": 0, "skipped": 0, "reason": reason, "cooldown": False}

    if not force and cooldown_active(category):
        reason = f"cooldown active ({COOLDOWN_HOURS}h since last {category} advisory)"
        logger.info("Advisory for %s suppressed: %s", category, reason)
        return {"sent": 0, "skipped": 0, "reason": reason, "cooldown": True}

    recipients = database.fetch_users_in_window(24.0)
    if not recipients:
        reason = "no users inside their 24h service window"
        logger.info("Advisory for %s: %s", category, reason)
        return {"sent": 0, "skipped": 0, "reason": reason, "cooldown": False}

    adapter = channel or channels_module.get_channel()
    sent = 0
    skipped = 0

    for phone_number in recipients[:limit]:
        # Re-check per recipient: the list was read a moment ago, and a window
        # can close between reads.  Sending outside it is the one thing here
        # that would cost money.
        if not database.service_window_open(phone_number, 24.0):
            skipped += 1
            logger.info("Skipping %s — %s", phone_number[:6] + "XXXXXX",
                        "outside 24h service window — send skipped to stay free")
            continue

        language = normalise_language(database.get_user_language(phone_number))
        text = body or build_body(category, language)

        if dry_run:
            logger.info("[dry-run] would advise %s in %s", phone_number[:6] + "XXXXXX", language)
            sent += 1
            continue

        if adapter.send(phone_number, channels_module.OutboundReply(text=text)):
            sent += 1
        else:
            skipped += 1

    if not dry_run:
        database.log_advisory(category, body or build_body(category, "en"), sent, skipped)

    logger.warning(
        "Advisory for %s: sent=%d skipped=%d (free window only)",
        category,
        sent,
        skipped,
    )
    return {"sent": sent, "skipped": skipped, "reason": "ok", "cooldown": False}


def preview(category: str) -> dict[str, str]:
    """The advisory copy in every supported language, for review."""
    return {
        language: build_body(category, language) for language in SUPPORTED_LANGUAGES
    }


def _cli() -> int:
    import sys  # noqa: PLC0415

    if len(sys.argv) < 2:
        print("usage: python advisory.py <category> [--dry-run] [--force]")
        return 2
    category = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    for language, text in preview(category).items():
        print(f"--- {language} ---\n{text}\n")
    print(dispatch(category, dry_run=dry_run, force=force))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    raise SystemExit(_cli())
