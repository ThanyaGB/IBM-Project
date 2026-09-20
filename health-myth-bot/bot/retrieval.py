"""Corpus retrieval: lexical BM25 + optional local multilingual embeddings.

Why this replaced ChromaDB
--------------------------
Chroma's default embedding function is an English-only ONNX MiniLM model, so
Hindi and Kannada queries were embedded by a model that has never seen those
scripts — and were then filtered by a hardcoded L2 distance of ``1.4`` that
nobody had calibrated against data.  Anything outside that uncalibrated radius
fell through to the "no verified answer" reply.  On top of that, the store was
a committed binary directory and a required 100+ MB dependency.

This module keeps the retrieval in-process and hybrid:

* **Lexical BM25** over a per-record document built from the topic, the
  translated topics, the authored ``keywords`` list, and the myth/fact text.
  This works in every supported script with no model at all — it is the floor,
  not a fallback.
* **Dense similarity** from a local multilingual model, used when (and only
  when) its weights are already on disk.  Nothing here ever downloads a model
  at request time; ``python retrieval.py --download-model`` is the explicit
  opt-in.

Acceptance is *evidence-based* rather than a bare distance threshold: a record
is offered as an answer when an authored keyword for it actually appears in the
query (the strongest signal across languages), or when the hybrid score clears
a floor.  The failure this guards against is the one the old substring matcher
had — attaching the tetanus card to "can I drink water during pregnancy"
because both contain a common token.

Uncalibrated values
-------------------
``DENSE_FLOOR`` and ``BM25_SATURATION`` could not be calibrated against the
local multilingual model in this environment (the weights are not on disk), so
they are env-overridable and marked here rather than presented as measured.
The keyword-anchor path, which is what most queries take, needs no tuning.

Translations and review
-----------------------
Verified facts are source-attributed clinical guidance.  A machine-assisted
translation that a human has not read is a safety risk, not a localisation win,
so ``translations[lang]`` is used as *source text* only once its ``status`` is
``approved`` (see ``bot/review_corpus.py``).  Until then the English text is the
source and the model is instructed to translate faithfully — the reply is still
in the user's language either way.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from bot.language import SUPPORTED_LANGUAGES, normalise_language

logger = logging.getLogger(__name__)

CORPUS_PATH = os.environ.get("HEALTH_FACTS_PATH", os.path.join("data", "health_facts.json"))

# Hybrid weights.  Lexical carries most of the decision because the keywords
# are authored per fact; the dense score reorders and rescues paraphrases.
LEXICAL_WEIGHT = float(os.environ.get("RETRIEVAL_LEXICAL_WEIGHT", "0.6"))
DENSE_WEIGHT = float(os.environ.get("RETRIEVAL_DENSE_WEIGHT", "0.4"))

# BM25 score at which the lexical component saturates to 1.0.
BM25_SATURATION = float(os.environ.get("RETRIEVAL_BM25_SATURATION", "8.0"))

# Cosine similarity floors for the multilingual model.  See the module
# docstring: uncalibrated in this environment.
DENSE_FLOOR = float(os.environ.get("RETRIEVAL_DENSE_FLOOR", "0.55"))

# Minimum hybrid score to offer a record at all when no keyword anchored it.
MIN_SCORE = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.30"))

# Authored keywords that mean "this question is about that fact".  Short Latin
# tokens like "tt" are kept because as *whole tokens* they are unambiguous.
MIN_KEYWORD_LENGTH = 2

# Suffix stripping is only safe for Latin script; Indic words are matched as
# substrings instead (see _keyword_matches).
MIN_INDIC_KEYWORD_LENGTH = 3

# Token characters: Latin/digits plus the Devanagari and Kannada blocks, so
# combining marks stay attached to their base letters.
TOKEN_RE = re.compile(r"[0-9A-Za-z_\u0900-\u097F\u0C80-\u0CFF]+")
_INDIC_RE = re.compile(r"[\u0900-\u097F\u0C80-\u0CFF]")

DEFAULT_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
DEFAULT_CACHE_DIR = os.environ.get(
    "FASTEMBED_CACHE_DIR",
    os.path.join(
        os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp",
        "fastembed_cache",
    ),
)


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------
def tokenise(text: str) -> list[str]:
    """Tokenise Latin *and* Indic text.

    ``\\w`` is not usable here: Devanagari and Kannada vowel signs are
    combining marks, and ``str.isalnum()`` is False for them, so a ``\\w``
    tokenizer splits every Indic word into fragments.  The character class
    above includes both script blocks explicitly.
    """
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]


def token_counts(text: str) -> Counter:
    return Counter(tokenise(text))


def has_indic(text: str) -> bool:
    return bool(_INDIC_RE.search(text or ""))


def _keyword_matches(keyword: str, query_text: str, query_tokens: set[str]) -> bool:
    """Whether an authored keyword appears in the query.

    Latin keywords must match whole tokens.  Unicode-aware substring
    containment is off the table for Latin because that is exactly how "in"
    (from "Tetanus Toxoid in Pregnancy") used to match "online order" and
    "fit" used to match "benefit".
    """
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return False

    if has_indic(keyword):
        # Indic words inflect by adding suffixes, so substring containment is
        # the right test here (टीका / टीके / टीकाकरण).  Require a minimum
        # length so a 2-character fragment can't match everything.
        if len(keyword) < MIN_INDIC_KEYWORD_LENGTH:
            return False
        return keyword in query_text

    if len(keyword) < MIN_KEYWORD_LENGTH:
        return False
    parts = tokenise(keyword)
    if not parts:
        return False
    return all(part in query_tokens for part in parts)


# ---------------------------------------------------------------------------
# Embeddings (optional, local-only)
# ---------------------------------------------------------------------------
def model_is_cached(model_name: str = DEFAULT_MODEL, cache_dir: str | None = None) -> bool:
    """True when the model weights are already on disk.

    Checked before constructing anything, so a missing model degrades to
    lexical-only retrieval instead of triggering a surprise 0.22 GB download
    on the first user message.
    """
    cache = Path(cache_dir or DEFAULT_CACHE_DIR)
    if not cache.exists():
        return False
    slug = model_name.replace("/", "--").replace("_", "-").lower()
    for entry in cache.iterdir():
        if not entry.is_dir():
            continue
        if slug in entry.name.lower():
            # A snapshot directory with a real model file means it's usable.
            for file in entry.rglob("*"):
                if file.suffix in {".onnx", ".bin", ".onnx_data"}:
                    return True
    return False


class Embedder:
    """Thin wrapper over fastembed, loaded lazily and locally only."""

    def __init__(
        self, model_name: str = DEFAULT_MODEL, cache_dir: str | None = None
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._model = None
        self._available: bool | None = None

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = model_is_cached(self.model_name, self.cache_dir)
            if not self._available:
                logger.info(
                    "Multilingual embedding model not cached — running lexical-only "
                    "retrieval. Run 'python retrieval.py --download-model' to enable "
                    "dense retrieval (%s).",
                    self.model_name,
                )
        return self._available

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding  # noqa: PLC0415

            self._model = TextEmbedding(
                model_name=self.model_name, cache_dir=self.cache_dir
            )
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available or not texts:
            return []
        try:
            model = self._load()
            return [vector.tolist() for vector in model.embed(list(texts))]
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Embedding failed (%s) — degrading to lexical-only", exc)
            self._available = False
            return []


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
def load_corpus(path: str | os.PathLike | None = None) -> list[dict]:
    """Load and validate health_facts.json."""
    resolved = Path(path or CORPUS_PATH)
    if not resolved.exists():
        raise FileNotFoundError(f"corpus not found at {resolved.resolve()}")
    with resolved.open("r", encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list) or not records:
        raise ValueError(f"{resolved} did not contain a non-empty list of records")
    for record in records:
        for key in ("id", "topic", "common_myth", "verified_fact", "source", "category"):
            if key not in record:
                raise ValueError(f"corpus record {record.get('id')} is missing '{key}'")
    return records


def translation_status(record: dict, language: str) -> str:
    """Review status of a record's translation: approved | draft | missing."""
    language = normalise_language(language)
    if language == "en":
        return "approved"
    translation = (record.get("translations") or {}).get(language)
    if not translation:
        return "missing"
    status = (translation.get("status") or "draft").strip().lower()
    return status if status in {"approved", "draft", "rejected"} else "draft"


def localised_text(record: dict, language: str) -> tuple[str, str, bool]:
    """Return (myth, verified_fact, used_translation) as source text.

    An approved translation is used verbatim.  A draft one is deliberately
    *not* handed to the model as source text: an unreviewed translation of
    clinical guidance would become the authority for what the bot says.
    """
    language = normalise_language(language)
    if language != "en" and translation_status(record, language) == "approved":
        translation = record["translations"][language]
        return (
            translation.get("common_myth") or record["common_myth"],
            translation.get("verified_fact") or record["verified_fact"],
            True,
        )
    return record["common_myth"], record["verified_fact"], False


def localised_topic(record: dict, language: str) -> str:
    """Topic in `language` when available (used on card images)."""
    language = normalise_language(language)
    if language == "en":
        return record["topic"]
    topic = (record.get("topic_i18n") or {}).get(language)
    if topic and translation_status(record, language) == "approved":
        return topic
    return record["topic"]


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------
@dataclass
class RetrievedFact:
    record: dict
    score: float
    lexical: float
    dense: float
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def anchored(self) -> bool:
        """True when an authored keyword tied the query to this record."""
        return bool(self.matched_keywords)

    @property
    def topic(self) -> str:
        return self.record["topic"]

    @property
    def category(self) -> str:
        return self.record["category"]


class Retriever:
    """Hybrid BM25 + dense corpus retriever."""

    def __init__(
        self,
        corpus_path: str | os.PathLike | None = None,
        embedder: Embedder | None = None,
        records: Sequence[dict] | None = None,
    ) -> None:
        self.records: list[dict] = list(records) if records else load_corpus(corpus_path)
        self.embedder = embedder if embedder is not None else Embedder()
        self._build_index()

    # -- index --------------------------------------------------------------
    def _document(self, record: dict) -> str:
        """Everything searchable about a record, all languages at once.

        Mixing languages in one document is deliberate: a Kannada query should
        match a Kannada keyword, and an English query the English topic, without
        us having to detect language correctly first.
        """
        parts: list[str] = [record["topic"], record["category"]]
        for topic in (record.get("topic_i18n") or {}).values():
            parts.append(topic)
        parts.extend(record.get("keywords") or [])
        parts.append(record["common_myth"])
        parts.append(record["verified_fact"])
        for translation in (record.get("translations") or {}).values():
            parts.append(translation.get("common_myth") or "")
            parts.append(translation.get("verified_fact") or "")
        return "\n".join(p for p in parts if p)

    def _build_index(self) -> None:
        self._docs: list[Counter] = []
        self._lengths: list[int] = []
        document_frequency: Counter = Counter()

        for record in self.records:
            counts = token_counts(self._document(record))
            self._docs.append(counts)
            self._lengths.append(sum(counts.values()))
            for token in counts:
                document_frequency[token] += 1

        total = len(self.records) or 1
        self._idf: dict[str, float] = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in document_frequency.items()
        }
        self._avg_length = (sum(self._lengths) / total) if self._lengths else 1.0
        self._vectors: list[list[float]] = []
        self._encode_documents()

    def _encode_documents(self) -> None:
        """Embed every record once, when the model is available."""
        if not self.embedder.available:
            self._vectors = []
            return
        vectors = self.embedder.encode([self._document(r) for r in self.records])
        self._vectors = vectors
        logger.info(
            "Encoded %d corpus records with %s",
            len(vectors),
            self.embedder.model_name,
        )

    # -- search -------------------------------------------------------------
    def bm25(self, query_tokens: Iterable[str], doc_index: int) -> float:
        counts = self._docs[doc_index]
        length = self._lengths[doc_index] or 1
        k1, b = 1.5, 0.75
        score = 0.0
        for token in set(query_tokens):
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            idf = self._idf.get(token, 0.0)
            denominator = frequency + k1 * (1 - b + b * length / self._avg_length)
            score += idf * (frequency * (k1 + 1)) / denominator
        return score

    def search(
        self, query: str, top_k: int = 3, threshold: bool = True
    ) -> list[RetrievedFact]:
        """Rank corpus records for `query`, best first.

        With ``threshold=False`` the caller gets the raw ranking (used by the
        dashboard preview and the eval harness, where seeing the near misses
        matters more than filtering them).
        """
        query = (query or "").strip()
        if not query:
            return []

        tokens = tokenise(query)
        token_set = set(tokens)
        lowered = query.lower()
        dense_query: list[float] = []
        if self._vectors:
            encoded = self.embedder.encode([query])
            dense_query = encoded[0] if encoded else []

        results: list[RetrievedFact] = []
        for index, record in enumerate(self.records):
            matched = [
                keyword
                for keyword in (record.get("keywords") or [])
                if _keyword_matches(keyword, lowered, token_set)
            ]

            raw_bm25 = self.bm25(tokens, index)
            lexical = min(1.0, raw_bm25 / BM25_SATURATION) if BM25_SATURATION else 0.0
            dense = (
                cosine(dense_query, self._vectors[index]) if dense_query else 0.0
            )

            score = LEXICAL_WEIGHT * lexical + DENSE_WEIGHT * max(0.0, dense)
            # An authored keyword is direct evidence; weight it explicitly so
            # a single distinctive term ("polio", "ಎಚ್ಐವಿ") outranks a generic
            # incidental overlap.
            if matched:
                score += 0.25 * min(len(matched), 3) / 3

            if score <= 0:
                continue
            results.append(
                RetrievedFact(
                    record=record,
                    score=round(score, 4),
                    lexical=round(lexical, 4),
                    dense=round(dense, 4),
                    matched_keywords=matched,
                )
            )

        results.sort(key=lambda item: item.score, reverse=True)

        if not threshold:
            return results[:top_k]

        accepted: list[RetrievedFact] = []
        for result in results:
            if result.anchored:
                accepted.append(result)
            elif result.score >= MIN_SCORE and result.dense >= DENSE_FLOOR:
                accepted.append(result)
            if len(accepted) >= top_k:
                break
        return accepted

    def best(self, query: str) -> RetrievedFact | None:
        results = self.search(query, top_k=1)
        return results[0] if results else None

    def category_for(self, query: str) -> str:
        """Category of the best-matching record, or "General" when unsure."""
        best = self.best(query)
        return best.category if best else "General"

    def stats(self) -> dict[str, Any]:
        return {
            "records": len(self.records),
            "languages": list(SUPPORTED_LANGUAGES),
            "dense_enabled": bool(self._vectors),
            "embedding_model": self.embedder.model_name if self._vectors else None,
        }


# ---------------------------------------------------------------------------
# Module-level singleton (the app builds one index per process)
# ---------------------------------------------------------------------------
_retriever: Retriever | None = None


def get_retriever(*, reload: bool = False) -> Retriever:
    global _retriever  # pylint: disable=global-statement
    if _retriever is None or reload:
        _retriever = Retriever()
    return _retriever


def reset_retriever() -> None:
    global _retriever  # pylint: disable=global-statement
    _retriever = None


def search(query: str, top_k: int = 3) -> list[RetrievedFact]:
    return get_retriever().search(query, top_k=top_k)


# ---------------------------------------------------------------------------
# CLI: opt-in model download, and a quick relevance probe
# ---------------------------------------------------------------------------
def _download_model(model_name: str, cache_dir: str) -> int:
    print(f"Downloading {model_name} into {cache_dir} …")
    try:
        from fastembed import TextEmbedding  # noqa: PLC0415

        embedder = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        vectors = list(embedder.embed(["ಸಂಪೂರ್ಣ ಆರೋಗ್ಯ"]))
        print(f"Done — model loaded, {len(vectors[0])}-dimensional vectors.")
        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1


def _probe(queries: Sequence[str]) -> int:
    retriever = get_retriever()
    print(f"index: {retriever.stats()}\n")
    for query in queries:
        print(f"Q: {query}")
        ranked = retriever.search(query, top_k=3, threshold=False)
        if not ranked:
            print("   (no candidates)")
        for result in ranked:
            flag = "✓" if result.anchored else " "
            print(
                f" {flag} {result.score:>6.3f}  {result.topic:<32} "
                f"lex={result.lexical:.2f} dense={result.dense:.2f} "
                f"kw={result.matched_keywords[:4]}"
            )
        accepted = retriever.search(query, top_k=1)
        print(f"   → {'ACCEPT: ' + accepted[0].topic if accepted else 'REJECT (fallback)'}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--download-model",
        action="store_true",
        help="fetch the local multilingual embedding model (one-time, ~220 MB)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "query",
        nargs="*",
        help="optional queries to probe retrieval against the corpus",
    )
    args = parser.parse_args(argv)

    if args.download_model:
        return _download_model(args.model, args.cache_dir)

    queries = args.query or [
        "is the polio vaccine safe for my child",
        "can I drink water during pregnancy",
        "ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ",
        "nange jvara ide enu madali",
        "mujhe bukhar aur khaansi hai",
        "why does my online order say in transit",
    ]
    return _probe(queries)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
    raise SystemExit(main())
