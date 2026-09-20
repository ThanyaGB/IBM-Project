"""Retrieval and emergency evaluation harness.

Turns "the bot gives good answers" into numbers, so a corpus or keyword change
can be judged instead of argued about.  Three things are measured, because they
fail independently:

* **retrieval accuracy** — the labelled topic ranks first for queries that have
  an answer in the corpus;
* **refusal accuracy** — queries the corpus does *not* cover produce no answer
  rather than a confident wrong one (a wrong card is worse than "I don't know");
* **emergency precision/recall** — the circuit breaker fires on real emergencies
  and, just as importantly, does *not* fire on the ordinary questions that make
  up most traffic.

Language detection is scored too, since a wrong detection means the reply
arrives in a language the user cannot read.

Usage
-----
    python eval/run_eval.py
    python eval/run_eval.py --verbose
    python eval/run_eval.py --min-retrieval 0.95 --min-refusal 0.9

Exits non-zero when a metric drops below its threshold, so it works as a CI
gate on corpus changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from bot import language  # noqa: E402
from bot import retrieval  # noqa: E402
from bot import safety_voice  # noqa: E402

EVAL_SET = Path(__file__).with_name("eval_set.json")


def load_cases() -> tuple[list[dict], list[dict]]:
    with EVAL_SET.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["cases"], payload.get("emergency_cases", [])


def run_retrieval(cases: list[dict], verbose: bool) -> dict:
    retriever = retrieval.get_retriever()

    # The emergency circuit breaker runs before retrieval, so a query that
    # triggers it never reaches the corpus.  Scoring retrieval on those would
    # measure a code path the message does not take.
    emergency_routed = [c for c in cases if safety_voice.check_emergency(c["query"])[0]]
    cases = [c for c in cases if not safety_voice.check_emergency(c["query"])[0]]

    answerable = [c for c in cases if c.get("expected_topic")]
    refusals = [c for c in cases if not c.get("expected_topic")]

    correct = 0
    wrong: list[str] = []
    for case in answerable:
        # Accept anything the answering path would offer as a card.
        accepted = retriever.search(case["query"], top_k=1)
        got = accepted[0].topic if accepted else None
        if got == case["expected_topic"]:
            correct += 1
        else:
            wrong.append(f"  {case['query'][:52]:<52} expected {case['expected_topic']!r}, got {got!r}")

    refused = 0
    false_positives: list[str] = []
    for case in refusals:
        if not retriever.search(case["query"], top_k=1):
            refused += 1
        else:
            got = retriever.search(case["query"], top_k=1)[0].topic
            false_positives.append(f"  {case['query'][:52]:<52} wrongly matched {got!r}")

    detected = sum(
        1
        for case in cases
        if language.detect_language(case["query"]) == case["language"]
    )

    if verbose and wrong:
        print("\nretrieval misses:")
        print("\n".join(wrong))
    if verbose and false_positives:
        print("\nwrongly answered (should have refused):")
        print("\n".join(false_positives))

    return {
        "emergency_routed": len(emergency_routed),
        "answerable": len(answerable),
        "retrieval_correct": correct,
        "retrieval_accuracy": correct / len(answerable) if answerable else 0.0,
        "refusals_total": len(refusals),
        "refusals_correct": refused,
        "refusal_accuracy": refused / len(refusals) if refusals else 0.0,
        "language_correct": detected,
        "language_accuracy": detected / len(cases) if cases else 0.0,
        "misses": wrong,
        "false_positives": false_positives,
    }


def run_emergency(cases: list[dict], verbose: bool) -> dict:
    true_positive = false_positive = true_negative = false_negative = 0
    failures: list[str] = []

    for case in cases:
        expected = bool(case["emergency"])
        actual, _ = safety_voice.check_emergency(case["query"])
        if expected and actual:
            true_positive += 1
        elif expected and not actual:
            false_negative += 1
            failures.append(f"  MISSED    {case['query'][:60]}")
        elif not expected and actual:
            false_positive += 1
            failures.append(f"  FALSE HIT {case['query'][:60]}")
        else:
            true_negative += 1

    if verbose and failures:
        print("\nemergency mismatches:")
        print("\n".join(failures))

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive)
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative)
        else 1.0
    )
    return {
        "emergency_true_positive": true_positive,
        "emergency_false_positive": false_positive,
        "emergency_false_negative": false_negative,
        "emergency_precision": precision,
        "emergency_recall": recall,
        "emergency_mismatches": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--min-retrieval", type=float, default=0.90)
    parser.add_argument("--min-refusal", type=float, default=0.90)
    parser.add_argument("--min-language", type=float, default=0.90)
    parser.add_argument("--min-emergency-precision", type=float, default=0.95)
    parser.add_argument("--min-emergency-recall", type=float, default=1.0)
    args = parser.parse_args(argv)

    cases, emergency_cases = load_cases()
    metrics = {**run_retrieval(cases, args.verbose), **run_emergency(emergency_cases, args.verbose)}

    index = retrieval.get_retriever().stats()
    print(f"corpus: {index['records']} records · languages {index['languages']} · "
          f"dense={'on' if index['dense_enabled'] else 'off (lexical-only)'}")
    print(f"queries routed to the emergency path (skipped for retrieval): "
          f"{metrics['emergency_routed']}")
    print()
    print(f"retrieval accuracy     {metrics['retrieval_correct']:>3}/{metrics['answerable']:<3} "
          f"= {metrics['retrieval_accuracy']:.1%}")
    print(f"refusal accuracy       {metrics['refusals_correct']:>3}/{metrics['refusals_total']:<3} "
          f"= {metrics['refusal_accuracy']:.1%}")
    print(f"language detection     {metrics['language_correct']:>3}/{len(cases):<3} "
          f"= {metrics['language_accuracy']:.1%}")
    print(f"emergency precision    = {metrics['emergency_precision']:.1%} "
          f"(false hits: {metrics['emergency_false_positive']})")
    print(f"emergency recall       = {metrics['emergency_recall']:.1%} "
          f"(missed: {metrics['emergency_false_negative']})")

    thresholds = [
        ("retrieval accuracy", metrics["retrieval_accuracy"], args.min_retrieval),
        ("refusal accuracy", metrics["refusal_accuracy"], args.min_refusal),
        ("language detection", metrics["language_accuracy"], args.min_language),
        ("emergency precision", metrics["emergency_precision"], args.min_emergency_precision),
        ("emergency recall", metrics["emergency_recall"], args.min_emergency_recall),
    ]
    failures = [
        f"{name} {value:.1%} is below the {threshold:.1%} floor"
        for name, value, threshold in thresholds
        if value < threshold
    ]

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("OK: all evaluation floors met")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s — %(message)s")
    raise SystemExit(main())
