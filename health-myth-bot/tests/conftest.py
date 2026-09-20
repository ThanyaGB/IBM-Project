"""Shared test configuration.

Environment is set *before* any application module is imported: several modules
read settings at import time (``database.DB_PATH``, ``channels`` channel
selection), and setting them afterwards silently tests the wrong thing.

Two deliberate choices:

* **The suite runs offline.**  ``GEMINI_API_KEY`` is not set, so ``_generate``
  returns None without a network call and the deterministic fallback path is
  exercised.  Tests that need a real answer monkeypatch ``rag_engine._generate``.
* **Signature validation is off by default here, and tested explicitly
  elsewhere.**  ``tests/test_channels.py`` turns it on and forges both a valid
  and an invalid signature, so the security path is covered rather than skipped.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="hmb-tests-"))

os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["CARDS_DIR"] = str(_TMP / "cards")
os.environ["AUDIO_DIR"] = str(_TMP / "audio")
os.environ["CHANNEL"] = "twilio"
os.environ["VALIDATE_WEBHOOK_SIGNATURES"] = "false"
os.environ["VOICE_REPLIES"] = "false"
os.environ["BOT_ENABLED"] = "true"
# .env ships this as "false" for the old Twilio account; interactive button
# payloads are part of what the Cloud API migration must prove works.
os.environ["SUPPORTS_INTERACTIVE_MESSAGES"] = "true"
os.environ["RATE_LIMIT_PER_HOUR"] = "20"
# No answer backend on purpose: keeps the suite offline and deterministic.
# Set to empty rather than popped: app.py calls load_dotenv(), which would
# refill a popped variable from a developer's real .env and silently turn the
# suite into a live-API test (burning free-tier quota on every run). dotenv
# never overrides an existing variable, and empty is falsy in rag_engine.
os.environ["GEMINI_API_KEY"] = ""
os.environ["OLLAMA_BASE_URL"] = ""
os.environ["WHATSAPP_APP_SECRET"] = "test-app-secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "test-access-token"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234567890"
os.environ["TWILIO_ACCOUNT_SID"] = "ACtest"
os.environ["TWILIO_AUTH_TOKEN"] = "test-token"

import pytest  # noqa: E402

from bot import channels as channels_module  # noqa: E402
from bot import database  # noqa: E402

database.init_db()


@pytest.fixture(autouse=True)
def clean_state():
    """Empty every table between tests, so ordering cannot matter."""
    with database._get_conn() as conn:
        for table in (
            "logs",
            "feedback",
            "alerts",
            "flagged_myths",
            "sessions",
            "processed_messages",
            "pending_state",
            "service_windows",
            "settings",
            "advisories",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    channels_module.reset_channel_cache()
    yield
    channels_module.reset_channel_cache()


@pytest.fixture
def app_client():
    import app as app_module

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield client


@pytest.fixture
def retriever():
    from bot import retrieval

    return retrieval.get_retriever()
