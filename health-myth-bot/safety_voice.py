"""Deterministic emergency circuit breaker + voice transcription.

check_emergency() is pure keyword matching with zero LLM calls.
download_audio() and transcribe_audio() wrap external I/O with soft-fail
semantics: exceptions are caught, logged, and the caller gets an empty
string rather than an unhandled 500.
"""

import logging
import os
import tempfile

import requests

logger = logging.getLogger(__name__)

EMERGENCY_KEYWORDS: list[str] = [
    "chest pain",
    "chest ache",
    "heart attack",
    "high fever",
    "very high temperature",
    "severe bleeding",
    "heavy bleeding",
    "bleeding badly",
    "difficulty breathing",
    "can't breathe",
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
    "fit",
    "poisoning",
    "poisoned",
    "swallowed something",
    "overdose",
    "stroke",
    "sudden numbness",
    "sudden weakness",
    "choking",
]

EMERGENCY_MESSAGE = (
    "⚠️ *This sounds like a medical emergency.*\n\n"
    "Please do one of the following *immediately*:\n"
    "• 🚑 Call your local emergency services (e.g., 112 / 911 / 999)\n"
    "• 🏥 Go to the nearest clinic or emergency room right now\n"
    "• 📞 Call a trained health worker or community health volunteer\n\n"
    "Do not wait. Your safety is the most important thing.\n\n"
    "_This bot is for general health information only and cannot replace "
    "emergency medical care._"
)


def check_emergency(text: str) -> tuple[bool, str | None]:
    lower = text.lower()
    for kw in EMERGENCY_KEYWORDS:
        if kw in lower:
            logger.warning("Emergency keyword detected: '%s'", kw)
            return True, EMERGENCY_MESSAGE
    return False, None


def download_audio(media_url: str, auth: tuple, dest_path: str) -> str:
    response = requests.get(media_url, auth=auth, timeout=30, stream=True)
    response.raise_for_status()
    with open(dest_path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=8192):
            fh.write(chunk)
    logger.info("Audio downloaded to %s (%d bytes)", dest_path, os.path.getsize(dest_path))
    return dest_path


def transcribe_audio(audio_file_path: str) -> str:
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        logger.error("openai package not installed — cannot transcribe audio")
        _safe_delete(audio_file_path)
        return ""

    try:
        client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        with open(audio_file_path, "rb") as audio_fh:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_fh,
                response_format="text",
            )
        transcript = result.strip() if isinstance(result, str) else str(result).strip()
        logger.info("Whisper transcription success, length=%d chars", len(transcript))
        return transcript
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Whisper transcription failed: %s", exc)
        return ""
    finally:
        _safe_delete(audio_file_path)


def _safe_delete(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
