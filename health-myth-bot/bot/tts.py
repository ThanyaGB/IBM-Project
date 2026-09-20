"""Voice replies: free text-to-speech for the users who need it most.

Voice *input* already existed while voice *output* did not, which is backwards
for this product: the people sending voice notes are the least likely to read a
long written reply.

Backend
-------
``edge-tts`` — Microsoft Edge's online neural voices, free with no API key and
no billing, and it has native Hindi (``hi-IN-SwaraNeural``) and Kannada
(``kn-IN-SapnaNeural``) voices.  It is a network call, so it can fail; every
function here returns None instead of raising, and the caller sends the text
reply it already had.

WhatsApp only accepts ``audio/ogg`` with the Opus codec, so the MP3 that
edge-tts produces is transcoded with ffmpeg when ffmpeg is present.  Without
ffmpeg, ``synthesize`` returns None rather than uploading a format Meta will
reject — a voice reply that silently fails to arrive is worse than no voice
reply.

Nothing here is paid, and no key is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from bot.language import DEFAULT_LANGUAGE, normalise_language

logger = logging.getLogger(__name__)

AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "./static/audio"))
VOICE_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", "45"))
MAX_TTS_CHARS = int(os.environ.get("TTS_MAX_CHARS", "900"))

# One voice per supported language.  Kannada has two; Sapna is the female
# voice, which matches the maternal-health framing of most of the corpus.
VOICES: dict[str, str] = {
    "en": os.environ.get("TTS_VOICE_EN", "en-IN-NeerjaNeural"),
    "hi": os.environ.get("TTS_VOICE_HI", "hi-IN-SwaraNeural"),
    "kn": os.environ.get("TTS_VOICE_KN", "kn-IN-SapnaNeural"),
}

_FFMPEG = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


def voice_for(language: str) -> str:
    return VOICES.get(normalise_language(language), VOICES[DEFAULT_LANGUAGE])


def ffmpeg_available() -> bool:
    return bool(_FFMPEG and os.path.exists(_FFMPEG))


def edge_tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


def status() -> dict:
    return {
        "backend": "edge-tts",
        "available": edge_tts_available() and ffmpeg_available(),
        "edge_tts_installed": edge_tts_available(),
        "ffmpeg": _FFMPEG,
        "ffmpeg_available": ffmpeg_available(),
        "voices": dict(VOICES),
    }


def _cache_key(text: str, language: str) -> str:
    digest = hashlib.sha256(f"{language}:{text}".encode("utf-8")).hexdigest()[:16]
    return f"reply_{language}_{digest}"


async def _synthesize_mp3(text: str, voice: str, mp3_path: Path) -> None:
    import edge_tts  # noqa: PLC0415

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(mp3_path))


def _transcode_to_ogg(mp3_path: Path, ogg_path: Path) -> None:
    """MP3 → OGG/Opus, the only audio format WhatsApp accepts."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _FFMPEG,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(mp3_path),
            "-c:a",
            "libopus",
            "-b:a",
            "24k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-application",
            "voip",
            str(ogg_path),
        ],
        capture_output=True,
        timeout=VOICE_TIMEOUT,
        check=False,
    )
    if not ogg_path.exists():
        raise RuntimeError(
            f"ffmpeg produced no output (exit={result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:200]}"
        )


def synthesize(text: str, language: str = DEFAULT_LANGUAGE) -> str | None:
    """Render `text` as a WhatsApp-compatible OGG/Opus file.

    Returns the path, or None when TTS is unavailable or fails.  Never raises:
    a missing voice reply must not cost the user their answer.
    """
    language = normalise_language(language)
    text = (text or "").strip()
    if not text:
        return None

    if len(text) > MAX_TTS_CHARS:
        # Long replies are trimmed at a sentence boundary; the full text is
        # still sent as a normal message alongside the audio.
        text = text[:MAX_TTS_CHARS].rsplit(".", 1)[0] + "."

    if not edge_tts_available():
        logger.info("edge-tts not installed — skipping voice reply")
        return None
    if not ffmpeg_available():
        logger.info("ffmpeg not on PATH — skipping voice reply (WhatsApp needs OGG/Opus)")
        return None

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(text, language)
    ogg_path = (AUDIO_DIR / f"{key}.ogg").resolve()
    if ogg_path.exists():
        logger.info("Reusing cached voice reply %s", ogg_path.name)
        return str(ogg_path)

    mp3_path = AUDIO_DIR / f"{key}.mp3"
    try:
        asyncio.run(_synthesize_mp3(text, voice_for(language), mp3_path))
        _transcode_to_ogg(mp3_path, ogg_path)
        logger.info("Voice reply synthesized: %s", ogg_path.name)
        return str(ogg_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Voice reply synthesis failed: %s", exc)
        return None
    finally:
        try:
            mp3_path.unlink(missing_ok=True)
        except OSError:
            pass


def _cli() -> int:
    import sys  # noqa: PLC0415

    if len(sys.argv) > 1 and sys.argv[1] == "--voices":
        for code, voice in VOICES.items():
            print(f"{code}: {voice}")
        return 0
    text = sys.argv[1] if len(sys.argv) > 1 else "ನಿಮ್ಮ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ."
    language = sys.argv[2] if len(sys.argv) > 2 else "kn"
    print(status())
    print(synthesize(text, language))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    raise SystemExit(_cli())
