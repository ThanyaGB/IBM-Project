"""Retrieval-augmented rebuttal engine.

Flow
----
``get_myth_rebuttal(query, language)``

1. Retrieve the best-matching corpus records (``retrieval.py``: BM25 +
   optional local dense vectors) — script-aware, so Kannada and romanised
   Hindi queries are retrieved, not just English.
2. Build a prompt from the *source* text.  An approved translation is used
   verbatim; a draft one is deliberately excluded so unreviewed clinical
   wording can never become the bot's authority.
3. Generate the reply, asking for the answer in the user's language.  When
   the source is English the model is told to translate faithfully and add
   nothing — that is an explicit constraint, not an incidental side effect of
   asking for a language.
4. Append the source citation, and fall back to a localised "no verified
   answer" message whenever retrieval or generation fails.

Backends, in order
------------------
1. **Gemini** — free tier.  Note for deployment: on the free tier Google may
   use submitted content to improve its products, so this path is not suitable
   for anything a user would not accept being read.
2. **Ollama** — fully local and free (``OLLAMA_MODEL``, default ``llama3.2``).
   Set ``OLLAMA_BASE_URL`` to enable.  This is the recommended path for
   privacy-sensitive deployments and for demoing without internet.

There is no paid backend, and a failure in one never falls through to a
charge — it falls through to the other, then to the localised fallback text.
"""

from __future__ import annotations

import json
import logging
import os

from bot import retrieval
from bot.language import (
    DEFAULT_LANGUAGE,
    LANGUAGE_PROMPT_NAMES,
    SUPPORTED_LANGUAGES,
    detect_language,
    get_string,
    normalise_language,
    FALLBACK_MESSAGES,
)

__all__ = [
    "detect_language",
    "get_myth_rebuttal",
    "get_fallback",
    "init_retriever",
    "GEMINI_MODEL",
    "TOP_K",
]

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
TOP_K = int(os.environ.get("RAG_TOP_K", "3"))

# Health scope guard.  The bot is a health-education tool; answering
# unrelated questions ("write me a poem") wastes a free-tier request and
# dilutes the one thing it is trusted for.  The instruction is in the prompt
# rather than a hard classifier so a genuine question with unusual phrasing is
# still answered.
HEALTH_SCOPE_INSTRUCTION = (
    "If the question is not about health, medicine, pregnancy, child care, "
    "vaccination, nutrition or medication, reply only with one short sentence "
    "saying you can help with health questions, then stop. Do not answer the "
    "unrelated question."
)


def get_fallback(language: str = DEFAULT_LANGUAGE) -> str:
    """The localised "no verified answer" message."""
    return get_string(FALLBACK_MESSAGES, normalise_language(language))


def init_retriever(*, reload: bool = False) -> retrieval.Retriever:
    """Build (or rebuild) the corpus index.  Never downloads a model."""
    retriever = retrieval.get_retriever(reload=reload)
    logger.info("Retrieval index ready: %s", retriever.stats())
    return retriever


def _context_blocks(facts: list[retrieval.RetrievedFact], language: str) -> tuple[str, list[str]]:
    """Prompt context plus the source lines to cite."""
    blocks: list[str] = []
    sources: list[str] = []
    for index, fact in enumerate(facts, start=1):
        myth, verified_fact, used_translation = retrieval.localised_text(
            fact.record, language
        )
        topic = retrieval.localised_topic(fact.record, language)
        note = (
            f"  (already in {LANGUAGE_PROMPT_NAMES.get(language, language)})"
            if used_translation
            else "  (original English — translate faithfully)"
        )
        blocks.append(
            f"Record {index}:{note}\n"
            f"  Topic: {topic}\n"
            f"  Common myth: {myth}\n"
            f"  Verified fact: {verified_fact}"
        )
        sources.append(fact.record["source"])
    return "\n\n".join(blocks), sources


def _build_prompt(query: str, context_text: str, language: str) -> str:
    language_name = LANGUAGE_PROMPT_NAMES.get(
        normalise_language(language), "English"
    )
    return f"""You are a compassionate community health educator helping people in underserved communities understand health facts.

A community member has asked: "{query}"

Here are the verified health facts relevant to their question:

{context_text}

Instructions:
- Respond ONLY in {language_name}. Do not mix languages. If the verified facts are in English above, translate them faithfully into {language_name} — keep every claim, number and caution exactly, and add nothing that is not in the facts.
- Use warm, empathetic, everyday language. Avoid clinical jargon.
- Gently correct any myths without shaming or judging the person asking.
- Base your answer STRICTLY on the verified facts provided above. Do not invent statistics or make claims not supported by the context.
- Keep the reply concise enough for a WhatsApp message (3-5 short paragraphs maximum).
- End with a single sentence of encouragement or reassurance.
- {HEALTH_SCOPE_INSTRUCTION}
- Do NOT include a citation or source line — that will be added automatically.

Write your response now:"""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
def _generate_gemini(prompt: str) -> str | None:
    """Gemini free tier.  None when unavailable or failing."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai  # noqa: PLC0415

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        )
        text = (response.text or "").strip()
        if not text:
            logger.warning("Gemini returned an empty response")
            return None
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Gemini API call failed: %s", exc)
        return None


def _generate_ollama(prompt: str) -> str | None:
    """Local Ollama generation.  None when unconfigured or failing."""
    if not OLLAMA_BASE_URL:
        return None
    try:
        import requests  # noqa: PLC0415

        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=int(os.environ.get("OLLAMA_TIMEOUT", "120")),
        )
        response.raise_for_status()
        text = (response.json() or {}).get("response", "").strip()
        if not text:
            logger.warning("Ollama returned an empty response")
            return None
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Ollama call failed: %s", exc)
        return None


def _generate(prompt: str) -> str | None:
    """Try each free backend in order.  None when all fail."""
    for backend, name in (
        (_generate_gemini, f"gemini ({GEMINI_MODEL})"),
        (_generate_ollama, f"ollama ({OLLAMA_MODEL})"),
    ):
        text = backend(prompt)
        if text:
            logger.info("Rebuttal generated by %s", name)
            return text
    logger.error("Every generation backend failed")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_myth_rebuttal(query: str, target_language: str) -> str:
    """Answer `query` in `target_language`, grounded in the corpus."""
    language = normalise_language(target_language)

    try:
        retriever = retrieval.get_retriever()
        facts = retriever.search(query, top_k=TOP_K)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Retrieval failed: %s", exc)
        return get_fallback(language)

    if not facts:
        logger.info("No corpus record matched %r — using fallback", query[:60])
        return get_fallback(language)

    context_text, sources = _context_blocks(facts, language)
    prompt = _build_prompt(query, context_text, language)

    rebuttal_text = _generate(prompt)
    if not rebuttal_text:
        return get_fallback(language)

    unique_sources = list(dict.fromkeys(s for s in sources if s))
    citation = "📚 _Sources: " + "; ".join(unique_sources) + "_"
    return f"{rebuttal_text}\n\n{citation}"


def corpus_preview(query: str, top_k: int = 3) -> list[dict]:
    """Ranked candidates for `query`, for the dashboard and eval harness.

    Unlike the answering path this does not filter, so a reviewer can see the
    near misses and judge whether the corpus or the keywords need work.
    """
    retriever = retrieval.get_retriever()
    return [
        {
            "topic": result.topic,
            "category": result.category,
            "score": result.score,
            "lexical": result.lexical,
            "dense": result.dense,
            "matched_keywords": result.matched_keywords,
            "accepted": result.anchored or result.score >= retrieval.MIN_SCORE,
        }
        for result in retriever.search(query, top_k=top_k, threshold=False)
    ]


def status() -> dict:
    """Backend and index status, surfaced in the dashboard."""
    retriever = retrieval.get_retriever()
    return {
        "retrieval": retriever.stats(),
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "gemini_model": GEMINI_MODEL,
        "ollama_configured": bool(OLLAMA_BASE_URL),
        "ollama_model": OLLAMA_MODEL if OLLAMA_BASE_URL else None,
        "supported_languages": list(SUPPORTED_LANGUAGES),
    }


def translation_report() -> list[dict]:
    """Per-record, per-language review status for the dashboard."""
    rows: list[dict] = []
    retriever = retrieval.get_retriever()
    for record in retriever.records:
        row = {"id": record["id"], "topic": record["topic"]}
        for language in SUPPORTED_LANGUAGES:
            row[language] = retrieval.translation_status(record, language)
        rows.append(row)
    return rows


def _cli() -> int:
    import sys  # noqa: PLC0415

    queries = sys.argv[1:] or ["is the polio vaccine safe for my child"]
    for query in queries:
        print(json.dumps(corpus_preview(query), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s — %(message)s"
    )
    raise SystemExit(_cli())
