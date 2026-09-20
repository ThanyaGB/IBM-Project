"""Translation review gate for the health corpus.

``health_facts.json`` carries Hindi and Kannada translations of
``common_myth``/``verified_fact``.  Those are not UI copy: they are
source-attributed clinical guidance, and a machine-assisted translation that
nobody has read would become the authority for what the bot tells people about
vaccines and HIV.  So a translation is only used as source text once a human has
approved it — ``retrieval.localised_text`` enforces that, and this CLI is how a
reviewer approves.

Until a translation is approved the answer is still given in the user's
language: the model receives the English source and is instructed to translate
faithfully and add nothing.

Usage
-----
    python review_corpus.py status
    python review_corpus.py show 3 kn
    python review_corpus.py approve 3 kn --reviewer "Dr A. Rao"
    python review_corpus.py reject 3 kn --reviewer "Dr A. Rao" --note "dose wording"
    python review_corpus.py flags --status pending

Every command except ``status``/``show``/``flags`` writes the corpus file, so
``--dry-run`` is available and a diff is printed when git is present.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from bot import retrieval
from bot.language import SUPPORTED_LANGUAGES

CORPUS_PATH = Path(retrieval.CORPUS_PATH)


def _load() -> list[dict]:
    with CORPUS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(records: list[dict], dry_run: bool) -> None:
    text = json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        print("--- dry run, not writing ---")
        print(text[:1200] + ("\n…" if len(text) > 1200 else ""))
        return
    CORPUS_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {CORPUS_PATH}")
    _show_diff()


def _show_diff() -> None:
    """Show what changed, when the corpus is in a git checkout."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "diff", "--stat", "--", os.path.relpath(CORPUS_PATH)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except Exception:  # pylint: disable=broad-except
        return
    if result.stdout.strip():
        print(result.stdout.strip())


def _find(records: list[dict], record_id: int) -> dict:
    for record in records:
        if int(record["id"]) == int(record_id):
            return record
    raise SystemExit(f"no corpus record with id {record_id}")


def cmd_status(_: argparse.Namespace) -> int:
    records = _load()
    header = f"{'id':>3}  {'topic':<32} " + " ".join(
        f"{code:>9}" for code in SUPPORTED_LANGUAGES
    )
    print(header)
    print("-" * len(header))
    for record in records:
        cells = " ".join(
            f"{retrieval.translation_status(record, code):>9}"
            for code in SUPPORTED_LANGUAGES
        )
        print(f"{record['id']:>3}  {record['topic'][:32]:<32} {cells}")

    pending = [
        (record["id"], code)
        for record in records
        for code in SUPPORTED_LANGUAGES
        if retrieval.translation_status(record, code) == "draft"
    ]
    print(f"\n{len(pending)} translation(s) awaiting review, {len(records)} records.")
    if pending:
        print("Draft translations are NOT shown to users; the English source is used "
              "and translated at answer time until they are approved.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    record = _find(_load(), args.id)
    print(f"# {record['id']} {record['topic']}  [{record['category']}]")
    print(f"\n## English (source of truth)\nMyth: {record['common_myth']}\n"
          f"Fact: {record['verified_fact']}")
    for language in args.languages:
        translation = (record.get("translations") or {}).get(language)
        print(f"\n## {language} (status={retrieval.translation_status(record, language)})")
        if not translation:
            print("(no translation)")
            continue
        print(f"Myth: {translation.get('common_myth')}\n"
              f"Fact: {translation.get('verified_fact')}\n"
              f"Reviewer: {translation.get('reviewer')}  "
              f"Reviewed at: {translation.get('reviewed_at')}")
    return 0


def _decide(args: argparse.Namespace, status: str) -> int:
    records = _load()
    record = _find(records, args.id)
    translations = record.setdefault("translations", {})
    translation = translations.get(args.language)
    if translation is None:
        raise SystemExit(
            f"record {args.id} has no {args.language} translation to review"
        )

    previous = retrieval.translation_status(record, args.language)
    translation["status"] = status
    translation["reviewer"] = args.reviewer
    translation["reviewed_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if args.note:
        translation["review_note"] = args.note
    translation["method"] = (
        f"human-reviewed ({status})" if status == "approved" else "machine-assisted draft"
    )

    print(
        f"record {args.id} ({record['topic']}) {args.language}: "
        f"{previous} → {status} (reviewer={args.reviewer!r})"
    )
    _save(records, args.dry_run)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, "approved")


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, "rejected")


def cmd_flags(args: argparse.Namespace) -> int:
    """Flagged myths from real users — the other half of the review loop."""
    from bot import database  # noqa: PLC0415

    rows = database.fetch_flagged_myths(args.status)
    if not rows:
        print(f"no {args.status} flags")
        return 0
    for row in rows:
        print(
            f"[{row['id']}] {row['status']:<8} {row['language']:<3} "
            f"{row['timestamp']}  {row['query_text'][:80]}"
        )
        if row.get("review_note"):
            print(f"      note: {row['review_note']}")
    print(f"\n{len(rows)} flag(s). Resolve them in the Streamlit dashboard.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Health corpus translation review")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="translation status per record").set_defaults(
        func=cmd_status
    )

    show = sub.add_parser("show", help="print one record's text for review")
    show.add_argument("id", type=int)
    show.add_argument("languages", nargs="*", default=list(SUPPORTED_LANGUAGES))
    show.set_defaults(func=cmd_show)

    for name, func in (("approve", cmd_approve), ("reject", cmd_reject)):
        decision = sub.add_parser(name, help=f"mark a translation {name}d")
        decision.add_argument("id", type=int)
        decision.add_argument("language", choices=list(SUPPORTED_LANGUAGES))
        decision.add_argument("--reviewer", required=True)
        decision.add_argument("--note", default=None)
        decision.add_argument("--dry-run", action="store_true")
        decision.set_defaults(func=func)

    flags = sub.add_parser("flags", help="list flagged myths")
    flags.add_argument("--status", default="pending",
                       choices=["pending", "approved", "rejected", "all"])
    flags.set_defaults(func=cmd_flags)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
