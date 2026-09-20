# IBM Bob Usage — Health Myth-Bot SDLC Write-Up

> This document describes concretely — not generically — how IBM Bob was used as an AI-assisted engineering partner across every phase of the Software Development Life Cycle (SDLC) for the **health-myth-bot** project.

---

## 1. Requirements Analysis and Architecture Design

**What Bob did:**
Bob was given the hackathon brief and asked to reason about the optimal architecture for a multilingual, safety-critical WhatsApp health bot. Specifically:

- Bob proposed the **deterministic safety circuit breaker** design (`safety_voice.check_emergency()`) as a non-LLM component and explained *why* it must never rely on an LLM: an LLM can hallucinate, misclassify, or time out — none of which are acceptable when someone might be experiencing a cardiac event. Bob articulated this reasoning in the design document and inline in code comments, making the architectural decision auditable.

- Bob designed the **privacy contract** between `database.py` and the rest of the system: raw phone numbers in `sessions` only (routing necessity), SHA-256 hashed `anonymized_hash` in all other tables. Bob flagged that `log_query()` must hash the number before any write and that dashboard displays must never render raw identifiers.

- Bob produced the ASCII architecture diagram in `README.md` showing the layered flow from the channel adapter → Flask → safety check → RAG → LLM → SQLite, and iterated on it when the stretch goals (feedback loop, flagging, alerts) changed the data flow.

- Bob specified the **channel-agnostic architecture** contract: `app.py` deals only in `InboundMessage` / `OutboundReply` dataclasses; every provider-specific detail lives in `channels.py`. Switching from the Twilio legacy adapter to the WhatsApp Cloud API is a single `CHANNEL=whatsapp` config change, not a code change.

- Bob reasoned through the **free-by-construction cost model**: every outgoing message is a reply to an inbound message, sent inside the 24-hour customer-service window that message opened. Inbound is never charged and non-template replies inside the window are free. This is enforced by architecture (not by a budget alert), and Bob documented it in `app.py`'s module docstring and in `advisory.py`.

---

## 2. Scaffolding the Flask + Channel Webhook Boilerplate

**What Bob did:**
Bob generated the initial `app.py` scaffold, specifically:

- The **channel-agnostic webhook handler**: parsing inbound messages through the `channels.Channel` protocol, which abstracts both the WhatsApp Cloud API (default) and the legacy Twilio adapter. Bob designed the `InboundMessage` dataclass (fields: `message_id`, `sender`, `kind`, `text`, `media_id`, `media_url`, `media_type`, `raw`) and the `OutboundReply` dataclass (fields: `text`, `image_path`, `audio_path`, `buttons`) so that `app.py` never needs to know which channel is live.

- The **WhatsApp Cloud API channel** (`WhatsAppCloudChannel` in `channels.py`): Bob implemented the Meta webhook verification handshake (GET + `hub.challenge`), the `X-Hub-Signature-256` HMAC verification, the Graph API media download, and the interactive button payload builder — the `_interactive_payload()` that structures `👍 Helpful` / `👎 Not helpful` / `Yes, report it` / `No thanks` as native WhatsApp interactive list messages when `SUPPORTS_INTERACTIVE_MESSAGES=true`.

- The **duplicate-delivery guard**: providers retry webhooks on any non-2xx or network timeout. Bob added `database.is_message_processed()` / `database.mark_message_processed()` so each `message_id` is idempotent — the LLM is never called twice for the same delivery, and the question is never double-logged.

- The **environment variable fail-fast block**: Bob added `_require_at_least_one_generator()` (warns at boot when neither `GEMINI_API_KEY` nor `OLLAMA_BASE_URL` is set) and `_verify_channel_credentials()` (checks the four WhatsApp Cloud API variables or the two Twilio variables, depending on `CHANNEL`). Both run at import time — before the first request — so the server surfaces bad configuration as structured `ERROR` log lines rather than a traceback on the first real user message.

- The **`/health` endpoint**: returns a JSON inventory of what is configured, degraded, or disabled — including `answer_backend`, `indic_card_shaping`, `voice_replies`, and `bot_enabled`. Bob designed this as an honest uptime probe: every degraded component is named, not hidden.

- The **structured request flow** in `_handle_inbound()`, maintaining the invariant that `check_emergency()` always runs first, before the kill-switch check, rate limit, or any LLM call. Bob flagged that moving this check even one step later would add latency to the emergency response path and that someone in crisis must never be told to slow down.

---

## 3. Multi-File Contextual Refactoring

This was the highest-value Bob interaction in the project. When the stretch goals were added to the spec, multiple files needed coordinated changes:

**Cross-file change set 1 — Feedback loop:**
- Bob held `database.py`, `app.py`, and `dashboard.py` in context simultaneously.
- Added `feedback` table DDL to `database.init_db()`.
- Added `record_feedback(phone_number, rating)` to `database.py` — Bob reasoned through the hashing requirement (must look up via `anonymized_hash`, not raw phone).
- Modified `app.py`'s `_handle_inbound()` to: (a) append feedback prompt after rebuttal, (b) handle the `👍`/`👎` reply state using `database.set_pending_state(sender, "feedback")`, and (c) call `database.record_feedback()`. Bob also wired interactive buttons (`_feedback_buttons()`) so tapping is equivalent to typing, with the button id arriving as the message text and resolved by `_resolve_button()`.
- Added `helpfulness_rate` key to `database.fetch_summary_stats()` and the corresponding metric card to `dashboard.py`.
- Bob verified that all call sites matched across files before finalising.

**Cross-file change set 2 — Flagged myths:**
- Added `flagged_myths` table and `database.flag_myth()` / `database.fetch_flagged_myths()` to `database.py`.
- Modified `app.py` to detect the low-confidence fallback response (using `_is_fallback_response()` which delegates to `language.is_fallback_response()`), store the query via `database.set_pending_state(sender, "flag", payload=body)`, and handle the subsequent interactive confirmation with `_flag_buttons()`.
- Added `review_corpus.py` — a CLI tool (`bot.review_corpus status/show/approve/reject`) for a human reviewer to act on flagged myths, promoting them to `health_facts.json` after approval.
- Added the flagged myths review queue section to `dashboard.py`.

**Cross-file change set 3 — Rumour spike alerting:**
- Created `alerting.py` with `detect_rumor_spike()`.
- Added `alerts` table and `database.log_alert()` / `database.fetch_recent_alerts()` to `database.py`.
- Wired `dashboard.py` to import from `alerting.py` and show the amber alert banner.

**Cross-file change set 4 — Shareable myth cards:**
- Refactored `card_generator.py` (Pillow-based, fallback for environments without a browser) and added `card_renderer.py` — a headless Chromium renderer that produces correctly shaped Devanagari and Kannada text. Bob designed the two-tier rendering strategy: the Chromium renderer is tried first (`render_card()`); if no browser is found, Pillow's `_render_with_pillow()` is used silently. The Pillow fallback renders English-only cards correctly; the Chromium path is required for correct Indic script shaping (Pillow without Raqm cannot reorder the `ि` vowel sign).
- Bob designed the content policy in `card_renderer.py`: `localised_text()` only returns a translation when a reviewer has approved it, so a draft translation never reaches a card. When approved, the card is bilingual — the user's language first, English wording underneath in muted text so a health worker can verify the claim.
- Modified `app.py` to expose `_answer_with_card()`: calls `retrieval.search(query, top_k=1)`, passes the best record to `generate_myth_card()`, and attaches the result as `image_path` on the `OutboundReply`. Card generation is non-fatal by design: any `Exception` from Pillow or Chromium is caught and logged as a `WARNING`, and the request continues.
- The WhatsApp Cloud API channel (`channels.py`) calls `upload_media()` to upload the PNG via the Graph API and reference it by media id — no public URL needed, unlike the legacy Twilio path.

**Cross-file change set 5 — Text-to-speech voice replies:**
- Created `tts.py` with `synthesize(text, language)` — uses `edge-tts` to produce an MP3, transcodes to OGG Opus via `ffmpeg` (the format WhatsApp requires for voice notes), and caches the result so the same answer text is never synthesized twice.
- Bob designed the voice-reply trigger: voice replies fire only when the user's inbound message was itself a voice note (`was_voice` flag in `_handle_inbound()`). The reply carries both `text` and `audio_path` so channels that support media can attach the note.
- `VOICE_REPLIES=true` in `.env`; silently skipped when `edge-tts` or `ffmpeg` is absent.

**Cross-file change set 6 — Advisory broadcasting:**
- Created `advisory.py` with `dispatch(category, ...)` — iterates `database.fetch_users_in_window()` (users who have messaged in the last 24 hours), checks per-category cooldown via `database.last_advisory_at()`, and sends a proactive health advisory through the channel adapter.
- Bob designed the cooldown guard so the same category advisory is never sent more often than `ADVISORY_COOLDOWN_HOURS` (default 12 h), and the service-window check ensures the bot never sends an unsolicited template to someone who hasn't messaged recently (which would incur a Meta template charge).

In each case, Bob traced the full call chain from trigger → function → table → dashboard widget and confirmed consistent signatures, argument types, and return shapes across files.

---

## 4. Generating the Deterministic Safety Circuit Breaker

**What Bob did:**
Bob designed `safety_voice.EMERGENCY_KEYWORDS` and `check_emergency()` with specific attention to:

- **Coverage breadth**: Bob expanded the initial keyword list to include synonyms and adjacent signals that field testers commonly use (`passed out`, `convulsing`, `fit`, `swallowed something`, `stopped breathing`), drawing on knowledge of how community health messages are typically phrased in informal language. Keywords are compiled to regex patterns at import time (`_COMPILED_KEYWORDS`) for O(1) per-message dispatch.

- **Multi-language support**: emergency keywords are indexed by language (`en`, `hi`, `kn`) and the compiled patterns normalise apostrophes and Unicode whitespace via `_normalise_emergency_text()` before matching, so regional spellings and copy-pasted Unicode don't escape detection.

- **Design reasoning**: Bob produced the explicit argument — documented in comments and this document — for why this function must be deterministic Python string matching, not an LLM call:
  1. Zero added latency (microseconds vs. 1–3 seconds for LLM)
  2. Zero hallucination risk
  3. Predictable, auditable behaviour for a safety-critical path
  4. Works even if `GEMINI_API_KEY` is invalid or the Gemini API is down

- **Soft-fail boundary**: Bob placed `check_emergency()` as the first operation in `_handle_inbound()`, *before* the kill-switch check, rate limit, and any I/O that could raise an exception, ensuring the emergency check cannot be skipped by an upstream error. The bot enabled/disabled state is explicitly not checked before the emergency path runs.

---

## 5. Hybrid Retrieval Engine (`retrieval.py`)

**What Bob did:**
Bob designed and implemented the retrieval layer in `retrieval.py` as a hybrid lexical + dense system:

- **BM25 (lexical)** gives high precision for exact-keyword matches; **cosine similarity over dense embeddings** (via `fastembed` + `paraphrase-multilingual-MiniLM-L12-v2`) gives recall for paraphrase and cross-lingual queries. The two scores are combined with configurable weights (`RETRIEVAL_LEXICAL_WEIGHT=0.6`, `RETRIEVAL_DENSE_WEIGHT=0.4`).

- Bob designed the **offline-first degradation**: the keyword/BM25 path needs no model download and no GPU, so the bot still gives useful answers in environments where the embedding model is not cached. The dense path activates automatically when the model is present.

- The `Retriever.search()` method returns `RetrievedFact` dataclasses carrying `score`, `lexical`, `dense`, and `matched_keywords`, making retrieval decisions fully inspectable in the evaluation harness.

- Bob added `retrieval.py --probe` and `--download-model` CLI subcommands so operators can verify the corpus is working and pre-cache the model without starting the full server.

---

## 6. Language Detection and Localisation (`language.py`)

**What Bob did:**
Bob consolidated all string tables and language-detection logic into a single module:

- Supported languages: English (`en`), Hindi (`hi`), Kannada (`kn`). The module exposes `SUPPORTED_LANGUAGES`, `MENU_DIGITS` (digit → language code), and localised string tables for every user-facing string (error replies, feedback prompts, flag prompts, rate-limit replies, bot-paused notices, audio-failure messages, and more).

- `detect_language()` uses a two-pass strategy: script-range counting (Devanagari / Kannada Unicode ranges) for Indic text, then `langdetect` for Latin-script text, with a romanised-marker scorer (`score_romanised()`) as a tiebreaker for transliterated Hindi/Kannada queries.

- Bob designed the `FALLBACK_MARKERS` tuple — substrings that identify the "consult your health worker" fallback response in any language — so `is_fallback_response()` can detect a low-confidence answer without inspecting the RAG score directly.

---

## 7. Generating `seed_data.py`

**What Bob did:**
- Bob generated realistic, culturally plausible query rows across English, Hindi, and Kannada — matching the topics in `health_facts.json` — without inventing phone numbers (using format placeholders).
- Bob made the seed script idempotent using a `SEED_MARKER` suffix in `query_text` so that re-runs clear and re-insert cleanly without accumulating duplicates.
- Bob added feedback rows, session rows, and a flagged myth row to ensure the dashboard shows all sections populated out-of-the-box.
- Bob verified that the direct `sqlite3` calls in `seed_data.py` were consistent with the column order in `database.py`'s DDL — critical because `seed_data.py` bypasses the helper functions and inserts via raw SQL.

---

## 8. Generating the Documentation Set

**What Bob did:**

- **README.md**: Bob structured the README to serve two audiences — judges evaluating the demo (demo script, architecture diagram, stretch-goals table) and developers setting up the project (step-by-step setup, ngrok/cloudflare instructions, known limitations).

- **IBM_BOB_USAGE.md** (this document): Bob drafted the initial outline of SDLC phases and fleshed out each with concrete, project-specific examples.

- **`.env.example`**: Bob enumerated every environment variable referenced anywhere in the codebase (including optional overrides) with one-line comments. The file now reflects the primary WhatsApp Cloud API channel, the Ollama local generation option, edge-tts + ffmpeg voice reply pipeline, headless Chromium card rendering, and the legacy Twilio channel kept for backward compatibility.

- **`security-review.md`**: Bob produced a prioritised issue list (see Section 10).

- **Inline code comments**: Throughout the codebase, Bob wrote decision-rationale comments at architectural branch points — e.g., why the duplicate-delivery guard runs before `_handle_inbound()`, why `advisory.py` checks `service_window_open()` before sending, why `card_renderer.py` uses a versioned filename (`RENDER_VERSION`) so re-answering the same question overwrites one file instead of accumulating PNGs.

---

## 9. CI / Evaluation Pipeline (`.github/workflows/ci.yml`)

**What Bob did:**
Bob created the full CI pipeline that runs on every push and pull request:

- **Byte-compile gate**: `python -m compileall -q .` catches syntax errors across every module before tests run.
- **Pytest suite** (`tests/`): Four test files covering app and channel behaviour (`test_app_and_channels.py`), demo endpoint (`test_demo.py`), language detection and safety keywords (`test_language_and_safety.py`), and retrieval and corpus (`test_retrieval_and_corpus.py`). All tests run fully offline — no secrets, no paid backends.
- **Evaluation gate** (`eval/run_eval.py --verbose`): fails the build when retrieval, refusal, language detection, or emergency precision/recall drop below their floors. Bob designed this so a corpus or keyword change cannot quietly make answers worse.
- **Translation review status** (`python -m bot.review_corpus status`): informational step that prints the count of pending/approved/rejected draft translations without failing the build.
- Matrix: Python 3.11 and 3.12.

---

## 10. Code Review, Consistency Checking, and Security Audit

After all files were generated, Bob performed a cross-file consistency audit and security pass:

- Verified every function called in `app.py` and `dashboard.py` exists in `database.py`, `rag_engine.py`, `retrieval.py`, or `safety_voice.py` with matching names, argument order, and types.
- Confirmed `database.log_query(phone_number, query_text, language, category, is_emergency)` is called with the same five keyword arguments in every call site in `app.py`.
- Confirmed `database.fetch_summary_stats()` returns a dict with `total_queries`, `active_languages`, `emergency_count`, and `helpfulness_rate` — all four keys consumed by `dashboard.py`.
- Confirmed `safety_voice.transcribe_audio(audio_file_path, language)` signature matches the call in `_download_and_transcribe()`.
- Confirmed `retrieval.get_retriever().category_for(body)` is the correct call (not a direct `rag_engine` helper) after the retrieval layer was extracted into its own module.

**Bug fixed — `ValueError` in `fetch_all_logs()`:**
Bob traced the crash to SQLite's built-in `convert_timestamp` converter, which splits stored values on `b" "`. Any timestamp written with a `T` separator or UTC offset caused `datepart, timepart = val.split(b" ")` to raise `ValueError: not enough values to unpack`. The fix was applied in three files:
- `database.py`: all timestamp columns declared `TEXT`; `_get_conn()` omits `detect_types=sqlite3.PARSE_DECLTYPES`; WAL journal mode added for concurrent read performance.
- `seed_data.py`: all writers use a single `_fmt()` / `_utcnow()` helper producing exactly `YYYY-MM-DD HH:MM:SS`.
- `alerting.py`: matching `_fmt()` helper.

**Prioritised security issue list Bob produced (see `security-review.md` for full detail):**

| Priority | Issue | Recommended fix |
|---|---|---|
| High | Webhook had no signature validation | Implemented: `X-Hub-Signature-256` verified in `WhatsAppCloudChannel.verify_signature()`; Twilio `RequestValidator` in `TwilioChannel.verify()`. Controlled by `VALIDATE_WEBHOOK_SIGNATURES=true`. |
| High | Dashboard password is single plaintext string | Acceptable for demo; must be hardened before internet-facing deployment |
| Medium | Whisper transcription is unbounded and unthrottled | `MAX_AUDIO_BYTES` env var caps download size; per-user rate limit provides a secondary guard |
| Medium | `NamedTemporaryFile(delete=False)` could leave orphans | Fixed: `_download_and_transcribe()` always calls `safe_delete()` in both the early-exit and normal paths |
| Low | Broad `except Exception` in webhook | Audited; split where appropriate |
| Low | `_find_best_fact_record` was a keyword scan, not vector search | Replaced: `_answer_with_card()` now calls `retrieval.search(query, top_k=1)` |
| Low | Missing `CARD_BASE_URL` silently skipped card attachment | Resolved by the Cloud API channel: cards are uploaded via `upload_media()` and referenced by media id, so no public URL is needed |

Bob also confirmed: no hardcoded secrets, no `eval`/`os.system`/shell injection path, dashboard `unsafe_allow_html` built from static templates only (low XSS risk), and the privacy contract (raw phone numbers in `sessions` only; SHA-256 hash everywhere else) working as intended.

---

## 11. Specific IBM Bob Capabilities Used

| Bob Capability | How it was used in this project |
|---|---|
| Multi-file context window | Held `app.py` + `database.py` + `dashboard.py` + `channels.py` in context for cross-cutting changes (feedback loop, flagging, alerts, channel abstraction) |
| Code generation with constraints | Generated all stub-free, fully-implemented functions respecting the exact signatures in the spec |
| Architecture reasoning | Argued for the deterministic safety circuit breaker; designed the PII anonymisation contract; designed the free-by-construction cost model |
| Iterative refinement | Revised `rag_engine.py` prompt template after reasoning about multilingual tone requirements; replaced Pillow-only card rendering with the Chromium + Pillow fallback two-tier strategy |
| Documentation generation | Produced `README.md`, `.env.example`, `seed_data.py`, and this document |
| Consistency audit | Cross-checked function names, signatures, and return types across all Python source files after the retrieval layer was extracted |
| Scaffolding | Generated Flask + WhatsApp Cloud API webhook boilerplate, Streamlit dashboard layout, hybrid BM25 + dense retrieval pattern, advisory dispatch loop |
| Soft-fail design | Identified every external I/O call (Whisper, Gemini, ChromaDB/fastembed, edge-tts, ffmpeg, Graph API media upload) that needed `try/except` + graceful fallback |
| Startup validation | Added `_require_at_least_one_generator()` and `_verify_channel_credentials()` — structured log errors at boot time rather than runtime tracebacks |
| Image generation scaffold | Designed `card_renderer.py` (Chromium + correct Indic shaping) and `card_generator.py` (Pillow fallback), with the Cloud API media-upload path eliminating the need for a public `CARD_BASE_URL` |
| Security audit | Diagnosed and fixed SQLite timestamp crash; produced `security-review.md`; implemented `X-Hub-Signature-256` and Twilio `RequestValidator` webhook validation |
| CI / evaluation pipeline | Designed the offline test suite, the evaluation gate with precision/recall floors, and the GitHub Actions matrix (Python 3.11 + 3.12) |
| Voice pipeline | Designed `tts.py` (edge-tts → ffmpeg → OGG Opus), the voice-reply trigger tied to inbound voice notes, and the `transcribe_audio()` multi-backend chain (faster-whisper → OpenAI Whisper → Gemini) |

---

*This document was produced in collaboration with IBM Bob as part of the health-myth-bot IBM Hackathon submission.*
