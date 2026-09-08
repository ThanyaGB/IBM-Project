"""ChromaDB vector store + Gemini LLM rebuttal engine.

Responsibilities:
1. init_vector_store() — load health_facts.json into a persistent ChromaDB
   collection (idempotent upserts keyed on JSON id).
2. get_myth_rebuttal() — RAG query → Gemini prompt → response + citation.
3. detect_language() — lightweight detection using langdetect.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CHROMA_DB_DIR = os.environ.get("CHROMA_DB_DIR", "./chroma_db")
HEALTH_FACTS_PATH = os.environ.get("HEALTH_FACTS_PATH", "health_facts.json")
COLLECTION_NAME = "health_facts"
SIMILARITY_THRESHOLD = 1.4
TOP_K = 3

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
LANGDETECT_MIN_WORDS = 3

_LANG_NAMES = {"en": "English", "hi": "Hindi", "sw": "Swahili"}

_FALLBACK_MESSAGES = {
    "en": (
        "I'm sorry, I couldn't find a verified answer for that topic in my database yet. "
        "For reliable health information, please consult your local health worker or visit "
        "the nearest clinic. 🏥"
    ),
    "hi": (
        "मुझे खेद है, मेरे डेटाबेस में अभी इस विषय का सत्यापित उत्तर नहीं मिला। "
        "विश्वसनीय स्वास्थ्य जानकारी के लिए, कृपया अपने स्थानीय स्वास्थ्य कार्यकर्ता से "
        "या नजदीकी क्लिनिक जाकर सलाह लें। 🏥"
    ),
    "sw": (
        "Samahani, sijaona jibu lililothibitishwa kwa mada hiyo kwenye hifadhidata yangu bado. "
        "Kwa habari za afya zinazotegemewa, tafadhali wasiliana na mfanyakazi wa afya wa karibu "
        "nawe au tembelea kliniki iliyo karibu nawe. 🏥"
    ),
}


def _get_fallback(target_language: str) -> str:
    return _FALLBACK_MESSAGES.get(target_language, _FALLBACK_MESSAGES["en"])


def init_vector_store():
    import chromadb  # noqa: PLC0415

    facts_path = Path(HEALTH_FACTS_PATH)
    if not facts_path.exists():
        raise FileNotFoundError(f"health_facts.json not found at {facts_path.resolve()}")

    with facts_path.open("r", encoding="utf-8") as fh:
        facts: list[dict] = json.load(fh)

    client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "l2"},
    )

    ids = [str(f["id"]) for f in facts]
    documents = [f"{f['common_myth']} {f['verified_fact']}" for f in facts]
    metadatas = [
        {
            "topic": f["topic"],
            "common_myth": f["common_myth"],
            "verified_fact": f["verified_fact"],
            "source": f["source"],
            "category": f["category"],
        }
        for f in facts
    ]

    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    logger.info(
        "ChromaDB collection '%s' loaded with %d records (upserted from %s)",
        COLLECTION_NAME,
        len(facts),
        HEALTH_FACTS_PATH,
    )
    return collection


_collection = None


def _get_collection():
    global _collection  # pylint: disable=global-statement
    if _collection is None:
        _collection = init_vector_store()
    return _collection


def get_myth_rebuttal(query: str, target_language: str) -> str:
    try:
        collection = _get_collection()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("ChromaDB unavailable: %s", exc)
        return _get_fallback(target_language)

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(TOP_K, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("ChromaDB query failed: %s", exc)
        return _get_fallback(target_language)

    distances = results.get("distances", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]

    relevant = [
        meta for meta, dist in zip(metadatas, distances)
        if dist <= SIMILARITY_THRESHOLD
    ]

    if not relevant:
        logger.info(
            "No relevant ChromaDB match for query (min distance=%.3f, threshold=%.3f)",
            min(distances) if distances else 999,
            SIMILARITY_THRESHOLD,
        )
        return _get_fallback(target_language)

    context_blocks = []
    sources = []
    for i, meta in enumerate(relevant, start=1):
        context_blocks.append(
            f"Record {i}:\n"
            f"  Topic: {meta['topic']}\n"
            f"  Common myth: {meta['common_myth']}\n"
            f"  Verified fact: {meta['verified_fact']}"
        )
        sources.append(meta["source"])

    context_text = "\n\n".join(context_blocks)
    lang_name = _LANG_NAMES.get(target_language, target_language)

    prompt = f"""You are a compassionate community health educator helping people in underserved communities understand health facts.

A community member has asked: "{query}"

Here are the verified health facts relevant to their question:

{context_text}

Instructions:
- Respond ONLY in {lang_name}. Do not mix languages.
- Use warm, empathetic, everyday language. Avoid clinical jargon.
- Gently correct any myths without shaming or judging the person asking.
- Base your answer STRICTLY on the verified facts provided above. Do not invent statistics or make claims not supported by the context.
- Keep the reply concise enough for a WhatsApp message (3–5 short paragraphs maximum).
- End with a single sentence of encouragement or reassurance.
- Do NOT include a citation or source line — that will be added automatically.

Write your response now:"""

    try:
        from google import genai  # noqa: PLC0415

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        rebuttal_text = response.text.strip()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Gemini API call failed: %s", exc)
        return _get_fallback(target_language)

    unique_sources = list(dict.fromkeys(sources))
    citation = "📚 _Sources: " + "; ".join(unique_sources) + "_"
    return f"{rebuttal_text}\n\n{citation}"


def detect_language(text: str, fallback_language: str = "en") -> str:
    supported = {"en", "hi", "sw"}

    words = text.split()
    if len(words) < LANGDETECT_MIN_WORDS:
        logger.debug(
            "detect_language: text too short (%d words) — using fallback '%s'",
            len(words),
            fallback_language,
        )
        return fallback_language if fallback_language in supported else "en"

    try:
        from langdetect import detect, DetectorFactory  # noqa: PLC0415
        from langdetect.lang_detect_exception import LangDetectException  # noqa: PLC0415

        DetectorFactory.seed = 42
        detected = detect(text)
        if detected in supported:
            logger.debug("detect_language: detected '%s'", detected)
            return detected
        logger.debug(
            "detect_language: '%s' not in supported set — using fallback '%s'",
            detected,
            fallback_language,
        )
        return fallback_language if fallback_language in supported else "en"

    except ImportError:
        logger.warning("langdetect not installed — using fallback language '%s'", fallback_language)
        return fallback_language if fallback_language in supported else "en"
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("detect_language failed: %s — using fallback '%s'", exc, fallback_language)
        return fallback_language if fallback_language in supported else "en"
