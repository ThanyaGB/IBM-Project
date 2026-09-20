"""Website demo endpoints — landing page, config, and the emulator pipeline.

The demo endpoint is a thin, guarded wrapper around the real ``_handle_inbound``
pipeline, so these tests prove two things at once: the wrapper's guardrails
(input caps, session validation, the dashboard toggle) and that the wrapper
still behaves like the bot (real retrieval path, emergency short-circuit,
feedback pending-state).
"""

from __future__ import annotations

import json

import pytest

from bot import database
from bot import rag_engine

SESSION = "a1b2c3d4e5f60718"


def demo_post(client, message, session_id=SESSION, log_to_dashboard=None):
    payload = {"message": message, "session_id": session_id}
    if log_to_dashboard is not None:
        payload["log_to_dashboard"] = log_to_dashboard
    return client.post("/demo/message", json=payload)


def demo_reply(client, message, **kwargs):
    response = demo_post(client, message, **kwargs)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


# ---------------------------------------------------------------------------
# Landing page and config
# ---------------------------------------------------------------------------
def test_root_serves_the_landing_page(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Health Myth-Bot" in body
    # The landing page links to the standalone demo screen and carries its own
    # dashboard link ("in both places"), but hosts no emulator itself.
    assert 'href="/demo"' in body
    assert 'id="demoDashboardLink"' in body
    assert 'id="demoChat"' not in body


def test_demo_page_serves_the_emulator(app_client):
    response = app_client.get("/demo")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Live demo" in body
    # The emulator and its mirror toggle live here, not on the landing page.
    assert 'id="demoChat"' in body
    assert 'id="demoMirror"' in body
    assert 'id="demoDashboardLinkMain"' in body


def test_config_exposes_dashboard_url(app_client):
    payload = app_client.get("/demo/config").get_json()
    assert "dashboard_url" in payload
    assert payload["dashboard_url"].startswith("http")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_empty_message_is_rejected(app_client):
    assert demo_post(app_client, "   ").status_code == 400


def test_oversized_message_is_rejected(app_client):
    response = demo_post(app_client, "x" * 301)
    assert response.status_code == 400
    assert "too long" in response.get_json()["error"]


def test_max_length_message_is_accepted(app_client):
    assert demo_reply(app_client, "q" * 300)["reply_text"]


@pytest.mark.parametrize("bad_session", ["", "not hex!", "../etc/passwd", "a" * 65])
def test_invalid_session_id_is_rejected(app_client, bad_session):
    response = demo_post(app_client, "hello", session_id=bad_session)
    assert response.status_code == 400
    assert "session_id" in response.get_json()["error"]


def test_short_session_id_is_rejected(app_client):
    assert demo_post(app_client, "hello", session_id="abc").status_code == 400


# ---------------------------------------------------------------------------
# The wrapper behaves like the bot
# ---------------------------------------------------------------------------
def test_normal_question_runs_the_real_pipeline(app_client, monkeypatch):
    """The reply comes from the pipeline — the offline suite asserts the
    deterministic fallback, while a patched generator proves grounding."""
    monkeypatch.setattr(
        rag_engine, "get_myth_rebuttal",
        lambda **_: "A verified, grounded answer. 📚 _Sources: WHO_",
    )
    data = demo_reply(app_client, "is the polio vaccine safe for my child")
    assert "verified, grounded answer" in data["reply_text"]
    assert data["rate_limited"] is False
    assert data["image_url"] is None or data["image_url"].startswith("/static/cards/")
    # A successful answer asks for feedback, exactly as on WhatsApp.
    assert {b["id"] for b in data["buttons"]} == {"feedback_up", "feedback_down"}


def test_fallback_reply_offers_feedback_buttons(app_client):
    """No answer backend configured (the offline default) → the localised
    fallback fires and the bot asks whether the user wants to report a myth —
    with buttons, exactly as on WhatsApp."""
    data = demo_reply(app_client, "does garlic cure covid")
    assert data["reply_text"]
    assert any(b["id"] == "flag_yes" for b in data["buttons"])


def test_emergency_short_circuits_with_no_buttons(app_client):
    data = demo_reply(app_client, "my child is having a seizure what do I do")
    assert data["reply_text"]
    assert data["buttons"] == []
    # The emergency path must never produce a myth card.
    assert data["image_url"] is None


def test_feedback_button_tap_is_recorded(app_client, monkeypatch):
    monkeypatch.setattr(
        rag_engine, "get_myth_rebuttal", lambda **_: "A real answer."
    )
    first = demo_reply(app_client, "is the polio vaccine safe")
    assert any(b["id"] == "feedback_up" for b in first["buttons"])

    tapped = demo_reply(app_client, "feedback_up")
    assert "thank" in tapped["reply_text"].lower()
    with database._get_conn() as conn:
        rating = conn.execute("SELECT rating FROM feedback").fetchone()
    assert rating is not None and rating["rating"] == 1


# ---------------------------------------------------------------------------
# Dashboard-logging toggle
# ---------------------------------------------------------------------------
def test_toggle_off_logs_nothing(app_client, monkeypatch):
    monkeypatch.setattr(
        rag_engine, "get_myth_rebuttal", lambda **_: "A real answer."
    )
    before = database.fetch_all_logs(limit=1000)
    demo_reply(app_client, "is the polio vaccine safe", log_to_dashboard=False)
    assert database.fetch_all_logs(limit=1000) == before


def test_toggle_on_logs_one_row(app_client, monkeypatch):
    monkeypatch.setattr(
        rag_engine, "get_myth_rebuttal", lambda **_: "A real answer."
    )
    demo_reply(app_client, "is the polio vaccine safe", log_to_dashboard=True)
    logs = database.fetch_all_logs(limit=1000)
    assert len(logs) == 1
    row = logs[0]
    # The demo sender is hashed like any other user — no raw identifier.
    assert row["query_text"] == "is the polio vaccine safe"
    assert row["anonymized_hash"]


def test_default_is_to_log(app_client, monkeypatch):
    monkeypatch.setattr(
        rag_engine, "get_myth_rebuttal", lambda **_: "A real answer."
    )
    demo_reply(app_client, "is the polio vaccine safe")
    assert len(database.fetch_all_logs(limit=1000)) == 1


# ---------------------------------------------------------------------------
# Rate limit and hardening
# ---------------------------------------------------------------------------
def test_demo_rate_limit_returns_a_polite_reply(app_client, monkeypatch):
    import demo as demo_module

    monkeypatch.setattr(demo_module, "DEMO_RATE_LIMIT_PER_HOUR", 1)
    demo_reply(app_client, "is the polio vaccine safe", log_to_dashboard=True)
    data = demo_reply(app_client, "is the polio vaccine safe for my child")
    assert data["rate_limited"] is True
    assert data["reply_text"]


def test_pipeline_error_degrades_to_fallback_reply(app_client, monkeypatch):
    def boom(**_):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(rag_engine, "get_myth_rebuttal", boom)
    data = demo_reply(app_client, "is the polio vaccine safe")
    assert data["reply_text"]  # a reply, never a 500 / traceback
    assert data["rate_limited"] is False


def test_non_json_body_is_handled(app_client):
    response = app_client.post(
        "/demo/message",
        data="not json",
        content_type="application/json",
    )
    assert response.status_code == 400
