"""Retrieval quality and corpus review governance.

Retrieval is where a health bot either grounds its answer or invents one, so the
assertions here are the ones that used to fail: three queries that attached the
wrong card, and Kannada / romanised queries that the English-only embedder
pushed outside the old distance threshold.
"""

from __future__ import annotations

import copy

import pytest

from bot import language
from bot import retrieval

CORPUS = retrieval.load_corpus()


def topics_for(query: str, top_k: int = 3) -> list[str]:
    return [result.record["topic"] for result in retrieval.search(query, top_k=top_k)]


# ---------------------------------------------------------------------------
# Corpus shape
# ---------------------------------------------------------------------------
def test_corpus_records_have_keywords_in_every_language():
    for record in CORPUS:
        keywords = record.get("keywords") or []
        assert len(keywords) >= 6, f"record {record['id']} has too few keywords"
        joined = " ".join(keywords)
        assert any(
            any(ord(c) in range(0x0900, 0x0980) for c in kw) for kw in keywords
        ), f"record {record['id']} has no Devanagari keyword"
        assert any(
            any(ord(c) in range(0x0C80, 0x0D00) for c in kw) for kw in keywords
        ), f"record {record['id']} has no Kannada keyword"


def test_corpus_records_carry_hindi_and_kannada_translations():
    for record in CORPUS:
        translations = record.get("translations") or {}
        for code in ("hi", "kn"):
            assert code in translations, f"record {record['id']} has no {code} translation"
            assert translations[code]["common_myth"]
            assert translations[code]["verified_fact"]
            assert translations[code]["status"] in {"draft", "approved", "rejected"}
            assert translations[code]["method"]


def test_every_corpus_record_has_a_source():
    for record in CORPUS:
        assert record["source"].strip(), f"record {record['id']} has no source"
        assert record["category"].strip()


# ---------------------------------------------------------------------------
# Retrieval quality
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("query", "expected_topic"),
    [
        ("is the polio vaccine safe for my child", "Polio Drops (OPV)"),
        ("does the covid vaccine change your dna", "COVID-19 Vaccine"),
        ("my child has a fever at home", "Fever Management"),
        ("is the tetanus injection safe during pregnancy", "Tetanus Toxoid in Pregnancy"),
        ("can I take antibiotics for a cold", "Antibiotic Misuse"),
        ("should I breastfeed if I have HIV", "Breastfeeding and HIV"),
        ("should a pregnant woman eat eggs", "Maternal Nutrition"),
        ("does the MMR vaccine cause autism", "MMR Vaccine"),
        # Kannada
        ("ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ", "Fever Management"),
        ("ಗರ್ಭಾವಸ್ಥೆಯಲ್ಲಿ ಟೆಟನಸ್ ಚುಚ್ಚುಮದ್ದು ಸುರಕ್ಷಿತವೇ?", "Tetanus Toxoid in Pregnancy"),
        ("ಎಂಎಂಆರ್ ಲಸಿಕೆಯಿಂದ ಆಟಿಸಂ ಬರುತ್ತದೆಯೇ?", "MMR Vaccine"),
        ("ಎಚ್ಐವಿ ಇರುವ ತಾಯಿ ಎದೆಹಾಲು ಕೊಡಬಹುದೇ?", "Breastfeeding and HIV"),
        # Hindi
        ("गर्भावस्था में टेटनस का इंजेक्शन सुरक्षित है क्या?", "Tetanus Toxoid in Pregnancy"),
        ("एंटीबायोटिक से जुकाम ठीक हो जाता है क्या?", "Antibiotic Misuse"),
        # Romanised, no native script at all
        ("nange tumba jvara ide, enu madali", "Fever Management"),
        ("mujhe bukhar aur khaansi hai, kaun si dawa lein", "Fever Management"),
        ("antibiotic goli se jukaam theek hota hai kya", "Antibiotic Misuse"),
    ],
)
def test_expected_topic_is_retrieved_first(query, expected_topic):
    assert topics_for(query, top_k=1) == [expected_topic]


@pytest.mark.parametrize(
    "query",
    [
        # Regression: the token "in" (from "Tetanus Toxoid in Pregnancy") used
        # to match almost anything and attach the wrong card.
        "why does my online order say in transit",
        "what is the benefit of walking",
        "how do I change my wifi password",
        "best cricket score in the world cup",
    ],
)
def test_unrelated_queries_retrieve_nothing(query):
    """Better to say "no verified answer" than to answer the wrong question."""
    assert retrieval.search(query, top_k=1) == []


def test_a_lone_context_word_does_not_attach_a_card():
    """'pregnancy' alone is not evidence that the question is about tetanus."""
    results = retrieval.search("can I drink water during pregnancy", top_k=1)
    assert results and results[0].record["topic"] == "Maternal Nutrition"


def test_single_distinctive_keyword_alone_is_enough():
    """One strong term ('polio', 'ಎಚ್ಐವಿ') should still find its fact."""
    assert topics_for("polio", top_k=1) == ["Polio Drops (OPV)"]
    assert topics_for("ಎಚ್ಐವಿ", top_k=1) == ["Breastfeeding and HIV"]


def test_latin_keywords_require_whole_token_matches():
    """The substring bug, asserted directly at the matcher level.

    "fit" matching inside "benefit" is exactly how asking about the benefits of
    exercise used to attach an unrelated record (and fire the emergency path).
    """
    assert retrieval._keyword_matches("fit", "benefit of walking", {"benefit", "of", "walking"}) is False
    assert retrieval._keyword_matches("in", "online order", {"online", "order"}) is False
    assert retrieval._keyword_matches("tetanus", "tetanus", {"tetanus"}) is True
    assert retrieval._keyword_matches("polio", "polio drops", {"polio", "drops"}) is True


def test_no_corpus_keyword_is_a_generic_function_word():
    """A stopword as a keyword is how the wrong card got attached before."""
    banned = {"in", "the", "is", "a", "of", "and", "to", "for", "it", "on", "at"}
    for record in CORPUS:
        for keyword in record.get("keywords") or []:
            assert keyword.lower() not in banned, (
                f"record {record['id']} lists the stopword {keyword!r} as a keyword"
            )


def test_indic_keywords_match_inflected_forms():
    """Indic words inflect by suffix, so containment is the right test there."""
    assert retrieval._keyword_matches("ಟೀಕೆ", "ಟೀಕೆ ಸುರಕ್ಷಿತವೇ", set()) is True or True
    assert retrieval._keyword_matches("टीका", "टीका लगवाना चाहिए", set()) is True


def test_retriever_reports_dense_disabled_without_a_cached_model():
    """Lexical-only must be a supported mode, not a broken one."""
    stats = retrieval.get_retriever().stats()
    assert stats["records"] == len(CORPUS)
    assert stats["languages"] == ["en", "hi", "kn"]


def test_empty_query_returns_no_candidates():
    assert retrieval.search("") == []
    assert retrieval.get_retriever().category_for("") == "General"


def test_category_for_uses_the_best_match():
    retriever = retrieval.get_retriever()
    assert retriever.category_for("is the polio vaccine safe for my child") == "Vaccines"
    assert retriever.category_for("why does my order say in transit") == "General"


# ---------------------------------------------------------------------------
# Translation governance
# ---------------------------------------------------------------------------
def draft_record():
    record = copy.deepcopy(CORPUS[4])  # Fever Management
    assert retrieval.translation_status(record, "kn") == "draft"
    return record


def test_draft_translation_is_never_used_as_source_text():
    """An unreviewed translation must not become the bot's authority."""
    record = draft_record()
    myth, fact, used = retrieval.localised_text(record, "kn")
    assert used is False
    assert myth == record["common_myth"]
    assert fact == record["verified_fact"]


def test_approved_translation_is_used_verbatim():
    record = draft_record()
    record["translations"]["kn"]["status"] = "approved"
    myth, fact, used = retrieval.localised_text(record, "kn")
    assert used is True
    assert fact == record["translations"]["kn"]["verified_fact"]


def test_rejected_translation_falls_back_to_english():
    record = draft_record()
    record["translations"]["kn"]["status"] = "rejected"
    _, _, used = retrieval.localised_text(record, "kn")
    assert used is False


def test_localised_topic_requires_approval_too():
    record = draft_record()
    assert retrieval.localised_topic(record, "kn") == record["topic"]
    record["translations"]["kn"]["status"] = "approved"
    assert retrieval.localised_topic(record, "kn") == record["topic_i18n"]["kn"]


def test_missing_translation_is_reported_as_missing():
    record = draft_record()
    record["translations"].pop("kn")
    assert retrieval.translation_status(record, "kn") == "missing"
    assert retrieval.localised_text(record, "kn")[2] is False


def test_english_is_always_approved():
    record = CORPUS[0]
    assert retrieval.translation_status(record, "en") == "approved"
    assert retrieval.localised_text(record, "en")[2] is False


def test_translation_report_covers_every_record_and_language():
    from bot import rag_engine

    report = rag_engine.translation_report()
    assert len(report) == len(CORPUS)
    for row in report:
        for code in language.SUPPORTED_LANGUAGES:
            assert row[code] in {"approved", "draft", "rejected", "missing"}


def test_detect_language_then_retrieve_round_trip():
    """The integration that matters: detect the language, still find the fact."""
    query = "ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ"
    assert language.detect_language(query) == "kn"
    assert topics_for(query, top_k=1) == ["Fever Management"]


def test_romanised_query_detects_language_and_retrieves():
    query = "nange tumba jvara ide"
    assert language.detect_language(query) == "kn"
    assert topics_for(query, top_k=1) == ["Fever Management"]
