"""Shareable myth card renderer — HTML + headless Chromium.

Why Chromium
------------
The previous Pillow renderer drew text with ``draw.text``, which places glyphs
without shaping.  Devanagari and Kannada are complex scripts: they need
reordering (the ``ि`` vowel sign renders *before* its consonant) and ligature
substitution.  Pillow does that only when built against Raqm, and the Pillow in
this environment reports ``raqm: False`` — so Hindi cards were already broken,
and Kannada cards would be worse.  A browser shapes text correctly with the
system font stack and needs no extra C dependency.

Chromium is already present on this machine (Chrome and Edge), and the renderer
degrades to the old Pillow path when no browser is found, so card generation
never becomes a hard requirement for replying.

Content policy
--------------
The card shows the same text the answer is grounded in.  ``localised_text()``
returns a translation only when a reviewer has approved it, so a draft
translation never reaches a card.  When a translation *is* approved the card is
bilingual: the user's language first, the English wording underneath in muted
text, so a health worker can check the claim at a glance.

Output is deterministic: the card path is derived from the record id and the
renderer version, so re-answering the same question overwrites one file instead
of accumulating PNGs.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from bot import retrieval
from bot.language import (
    LANGUAGE_NAMES,
    normalise_language,
)

logger = logging.getLogger(__name__)

CARDS_DIR = Path(os.environ.get("CARDS_DIR", "./static/cards"))
CARD_WIDTH = 900
CARD_HEIGHT = 620
SCALE = float(os.environ.get("CARD_SCALE", "2"))
RENDER_TIMEOUT = int(os.environ.get("CARD_RENDER_TIMEOUT", "45"))
RENDER_VERSION = "3"

# Font stack: the first two cover Devanagari and Kannada on Windows and Linux
# respectively; the browser falls back per glyph, so a mixed-script card still
# renders completely.
FONT_STACK = (
    '"Nirmala UI", "Noto Sans Kannada", "Noto Sans Devanagari", '
    '"Tunga", "Segoe UI", Roboto, Arial, sans-serif'
)

_BROWSER_CANDIDATES = (
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

_browser_cache: str | None | bool = False


def find_browser() -> str | None:
    """Path to a usable Chromium-family browser, or None.

    ``CHROME_PATH`` wins when set, so a deployment can pin an exact binary.
    """
    global _browser_cache  # pylint: disable=global-statement
    if _browser_cache is not False:
        return _browser_cache  # type: ignore[return-value]

    found: str | None = None
    for candidate in _BROWSER_CANDIDATES:
        if not candidate:
            continue
        if os.path.exists(candidate):
            found = candidate
            break
    if found is None:
        for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
            located = shutil.which(name)
            if located:
                found = located
                break

    if found is None:
        logger.warning(
            "No Chromium-family browser found — card images will use the Pillow "
            "renderer, which cannot shape Devanagari or Kannada correctly. "
            "Install Chrome/Chromium or set CHROME_PATH."
        )
    else:
        logger.info("Card rendering via %s", found)
    _browser_cache = found
    return found


def reset_browser_cache() -> None:
    global _browser_cache  # pylint: disable=global-statement
    _browser_cache = False


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _block(
    label: str,
    label_class: str,
    primary: str,
    secondary: str | None,
) -> str:
    secondary_html = (
        f'<p class="secondary" lang="en">{_esc(secondary)}</p>'
        if secondary and secondary != primary
        else ""
    )
    return f"""
      <section class="block {label_class}">
        <h2>{_esc(label)}</h2>
        <p class="primary" lang="auto">{_esc(primary)}</p>
        {secondary_html}
      </section>"""


def build_card_html(
    record: dict,
    language: str = "en",
    myth: str | None = None,
    fact: str | None = None,
) -> str:
    """Render the card's HTML.

    `myth`/`fact` default to the *approved* localised text, so a draft
    translation can never leak onto a card.
    """
    language = normalise_language(language)
    localised_myth, localised_fact, used_translation = retrieval.localised_text(
        record, language
    )
    myth = myth if myth is not None else localised_myth
    fact = fact if fact is not None else localised_fact

    topic = retrieval.localised_topic(record, language)
    # Only show the English wording alongside when it differs, i.e. when the
    # card really is bilingual rather than an English card with a label.
    english_myth = record["common_myth"] if used_translation else None
    english_fact = record["verified_fact"] if used_translation else None

    language_label = LANGUAGE_NAMES.get(language, "English")
    badge = "✅ Health Myth-Bot · Verified"

    return f"""<!DOCTYPE html>
<html lang="{_esc(language)}">
<head>
<meta charset="utf-8">
<style>
  @page {{ margin: 0; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    width: {CARD_WIDTH}px;
    height: {CARD_HEIGHT}px;
    background: #f5f7f9;
    font-family: {FONT_STACK};
    color: #1a3a5c;
    display: flex;
    flex-direction: column;
  }}
  header {{
    background: #1a3a5c;
    border-top: 8px solid #1a7f7a;
    padding: 16px 32px 14px;
  }}
  header .eyebrow {{
    font-size: 13px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #9fd6d3;
    margin: 0 0 4px;
  }}
  header h1 {{
    margin: 0;
    font-size: 26px;
    color: #ffffff;
    line-height: 1.2;
  }}
  main {{
    flex: 1;
    display: flex;
    gap: 20px;
    padding: 22px 32px 0;
  }}
  .block {{
    flex: 1;
    border-radius: 12px;
    padding: 16px 18px;
    border: 2px solid;
  }}
  .block h2 {{
    margin: 0 0 10px;
    font-size: 14px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .block p {{ margin: 0; }}
  .block .primary {{ font-size: 18px; line-height: 1.45; }}
  .block .secondary {{
    margin-top: 12px;
    font-size: 13px;
    line-height: 1.4;
    opacity: 0.72;
    border-top: 1px dashed currentColor;
    padding-top: 10px;
  }}
  .myth {{ background: #fdf3f3; border-color: #c0392b; color: #7a3030; }}
  .myth h2 {{ color: #c0392b; }}
  .fact {{ background: #f0faf4; border-color: #27ae60; color: #1a4a2e; }}
  .fact h2 {{ color: #27ae60; }}
  footer {{
    padding: 16px 32px 20px;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
  }}
  .source {{ font-size: 12px; color: #57606a; line-height: 1.35; max-width: 620px; }}
  .badge {{
    background: #1a7f7a;
    color: #ffffff;
    font-size: 12px;
    font-weight: 600;
    padding: 6px 12px;
    border-radius: 6px;
    white-space: nowrap;
  }}
</style>
</head>
<body>
  <header>
    <p class="eyebrow">Health Myth-Bot · {_esc(record.get("category", ""))} · {_esc(language_label)}</p>
    <h1>{_esc(topic)}</h1>
  </header>
  <main>
    {_block("✗  Myth", "myth", myth, english_myth)}
    {_block("✓  Verified fact", "fact", fact, english_fact)}
  </main>
  <footer>
    <p class="source">📚 {_esc(record.get("source", ""))}</p>
    <span class="badge">{_esc(badge)}</span>
  </footer>
</body>
</html>
"""


def card_filename(record: dict, language: str = "en") -> str:
    """Stable filename per (record, language), so re-renders overwrite."""
    language = normalise_language(language)
    return f"myth_card_v{RENDER_VERSION}_{record['id']}_{language}.png"


def render_card(
    record: dict,
    language: str = "en",
    output_path: str | os.PathLike | None = None,
) -> str:
    """Render the card to a PNG and return its absolute path.

    Raises ``RuntimeError`` when no browser is available — the caller
    (``card_generator``) falls back to Pillow.
    """
    browser = find_browser()
    if not browser:
        raise RuntimeError("no Chromium-family browser available for card rendering")

    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(output_path) if output_path else CARDS_DIR / card_filename(
        record, language
    )
    out_path = out_path.resolve()

    html_text = build_card_html(record, language)
    with tempfile.TemporaryDirectory(prefix="hmb-card-") as work_dir:
        html_path = Path(work_dir) / "card.html"
        html_path.write_text(html_text, encoding="utf-8")

        profile_dir = Path(work_dir) / "profile"
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            # A throwaway profile: never touch the user's real browser profile.
            f"--user-data-dir={profile_dir}",
            f"--force-device-scale-factor={SCALE}",
            f"--window-size={CARD_WIDTH},{CARD_HEIGHT}",
            f"--screenshot={out_path}",
            html_path.as_uri(),
        ]
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            timeout=RENDER_TIMEOUT,
            check=False,
        )
        if not out_path.exists():
            raise RuntimeError(
                "browser produced no screenshot "
                f"(exit={result.returncode}): "
                f"{result.stderr.decode(errors='replace')[:300]}"
            )

    logger.info("Rendered card %s", out_path)
    return str(out_path)


def renderer_status() -> dict:
    """What the dashboard shows about card rendering."""
    browser = find_browser()
    return {
        "renderer": "chromium" if browser else "pillow",
        "browser": browser,
        "shapes_indic_correctly": bool(browser),
        "cards_dir": str(CARDS_DIR),
    }
