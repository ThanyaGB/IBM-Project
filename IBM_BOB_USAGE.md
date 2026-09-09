# IBM Bob Usage — Health Myth-Bot SDLC Write-Up

> This document describes concretely — not generically — how IBM Bob was used as an AI-assisted engineering partner across every phase of the Software Development Life Cycle (SDLC) for the **health-myth-bot** project.

---

## 1. Requirements Analysis and Architecture Design

**What Bob did:**
Bob was given the hackathon brief and asked to reason about the optimal architecture for a multilingual, safety-critical WhatsApp health bot. Specifically:

- Bob proposed the **deterministic safety circuit breaker** design (`safety_voice.check_emergency()`) as a non-LLM component and explained *why* it must never rely on an LLM: an LLM can hallucinate, misclassify, or time out — none of which are acceptable when someone might be experiencing a cardiac event. Bob articulated this reasoning in the design document and inline in code comments, making the architectural decision auditable.

- Bob designed the **privacy contract** between `database.py` and the rest of the system: raw phone numbers in `sessions` only (routing necessity), SHA-256 hashed `anonymized_hash` in all other tables. Bob flagged that `log_query()` must hash the number before any write and that dashboard displays must never render raw identifiers.

- Bob produced the ASCII architecture diagram in `README.md` showing the layered flow from Twilio → Flask → safety check → RAG → Gemini → SQLite, and iterated on it when the stretch goals (feedback loop, flagging, alerts) changed the data flow.

---

## 2. Scaffolding the Flask + Twilio Webhook Boilerplate

**What Bob did:**
Bob generated the initial `app.py` scaffold, specifically:

- The **Twilio webhook request format**: parsing `From`, `Body`, `MediaUrl0`, and `MediaContentType0` from Twilio's form-encoded POST, handling the fact that Twilio sends form data, not JSON.

- The **`_twiml_reply()` helper** that wraps a `MessagingResponse` and returns the XML string, along with the HTTP 200 status code — a subtlety that trips up many Twilio integrations (non-200 status causes Twilio to retry). Bob later extended this helper to accept an optional `media_url` parameter so that MMS myth-card attachments flow through the same return path without duplicating response-building logic.

- The **environment variable fail-fast block**: Bob insisted on checking all four required API keys at *import time* (not at first request) so the server refuses to start with a clear error message rather than returning a 500 on the first real user message.

- The **startup credential verification functions** `_verify_twilio_credentials()` and `_verify_google_genai_key()`: Bob added two soft-fail functions that make real API calls at boot time (Twilio Accounts endpoint and Gemini `models.list()`). Rather than surfacing an unhelpful traceback on the first user message, the server logs a clear `ERROR` if the Twilio Auth Token is wrong or rotated, or if the Gemini API key is invalid. Both functions degrade gracefully — a network failure or missing dependency produces a `WARNING`, not a startup crash.

- The **structured request flow** in `_handle_message()`, maintaining the invariant that `check_emergency()` always runs before any LLM call. Bob flagged that moving this check even one step later (e.g., after the ChromaDB retrieval) would add 1–3 seconds of latency to the emergency response path.

---

## 3. Multi-File Contextual Refactoring

This was the highest-value Bob interaction in the project. When the stretch goals (Sections 4.1–4.8) were added to the spec, multiple files needed coordinated changes:

**Cross-file change set 1 — Feedback loop (stretch 4.1):**
- Bob held `database.py`, `app.py`, and `dashboard.py` in context simultaneously.
- Added `feedback` table DDL to `database.init_db()`.
- Added `record_feedback(phone_number, rating)` to `database.py` — Bob reasoned through the hashing requirement (must look up via `anonymized_hash`, not raw phone).
- Modified `app.py`'s `_handle_message()` to: (a) append feedback prompt after rebuttal, (b) handle the `👍`/`👎` reply state using `_PENDING_FEEDBACK` dict, and (c) call `database.record_feedback()`.
- Added `helpfulness_rate` key to `database.fetch_summary_stats()` and the corresponding fourth metric card to `dashboard.py`.
- Bob verified that all four call sites matched across files before finalising — this is exactly the contextual multi-file reasoning that single-file tools cannot do.

**Cross-file change set 2 — Flagged myths (stretch 4.5):**
- Added `flagged_myths` table and `database.flag_myth()` / `database.fetch_flagged_myths()` to `database.py`.
- Modified `app.py` to detect the low-confidence fallback response (using `_is_fallback_response()`), stash the query in `_PENDING_FLAG`, and handle the subsequent `YES` confirmation.
- Added the flagged myths review queue section to `dashboard.py`.

**Cross-file change set 3 — Rumour spike alerting (stretch 4.4):**
- Created `alerting.py` with `detect_rumor_spike()`.
- Added `alerts` table and `database.log_alert()` / `database.fetch_recent_alerts()` to `database.py`.
- Wired `dashboard.py` to import from `alerting.py` and show the amber alert banner.

**Cross-file change set 4 — Shareable myth cards (stretch 4.6):**
- Created `card_generator.py` — a Pillow-based renderer that produces branded 800×500 PNG cards showing the myth, the verified fact, the source citation, and a "Health Myth-Bot | Verified" footer badge. Bob designed the cross-platform TTF font resolution (DejaVu → Liberation → Arial → Pillow built-in bitmap fallback) so cards render in minimal Linux containers as well as Windows dev machines.
- Modified `app.py` to import `generate_myth_card()`, added `_find_best_fact_record()` (keyword scan against `health_facts.json`) to locate the matching fact record for a query, and wired the generated card path into the optional `media_url` argument of `_twiml_reply()`.
- Added `CARD_BASE_URL` to `.env.example` as the publicly-accessible URL base for serving card assets (e.g. via the Cloudflare tunnel). If the variable is absent, card attachment is silently skipped — the rebuttal text is always delivered regardless.
- Card generation is non-fatal by design: any `Exception` from Pillow is caught, logged as a `WARNING`, and the request continues — consistent with the project's principle that no stretch-goal feature should ever drop a safety-critical reply.

In each case, Bob was able to trace the full call chain from trigger → function → table → dashboard widget and confirm consistent signatures, argument types, and return shapes — without asking the developer to manually cross-reference files.

---

## 4. Generating the Deterministic Safety Circuit Breaker

**What Bob did:**
Bob designed `safety_voice.EMERGENCY_KEYWORDS` and `check_emergency()` with specific attention to:

- **Coverage breadth**: Bob expanded the initial list (chest pain, high fever, not breathing) to include synonyms and adjacent signals that field testers commonly use (`passed out`, `convulsing`, `fit`, `swallowed something`, `stopped breathing`), drawing on knowledge of how community health messages are typically phrased in informal language.

- **Design reasoning**: Bob produced the explicit argument — documented in comments and this document — for why this function must be deterministic Python string matching, not an LLM call:
  1. Zero added latency (microseconds vs. 1–3 seconds for LLM)
  2. Zero hallucination risk
  3. Predictable, auditable behaviour for a safety-critical path
  4. Works even if GEMINI_API_KEY is invalid or the Gemini API is down

- **Soft-fail boundary**: Bob placed `check_emergency()` as the first operation in `_handle_message()`, *before* any I/O that could raise an exception, ensuring the emergency check cannot be skipped by an upstream error.

---

## 5. Generating seed_data.py

**What Bob did:**
- Bob generated 30 realistic, culturally plausible query rows across English, Hindi, and Swahili — matching the topics in `health_facts.json` — without inventing phone numbers (using format placeholders like `+91XXXXXXXXX1`).
- Bob made the seed script idempotent using a `SEED_MARKER` suffix in `query_text` so that re-runs clear and re-insert cleanly without accumulating duplicates.
- Bob added feedback rows, session rows, and a flagged myth row to ensure the dashboard shows all sections populated out-of-the-box.
- Bob verified that the direct `sqlite3` calls in `seed_data.py` were consistent with the column order in `database.py`'s DDL — critical because `seed_data.py` bypasses the ORM-like helpers and inserts directly via raw SQL.

---

## 6. Generating the Documentation Set

**What Bob did:**

- **README.md**: Bob structured the README to serve two audiences simultaneously — judges evaluating the demo (demo script, architecture diagram, stretch-goals table) and developers setting up the project (step-by-step setup, ngrok instructions, known limitations). The 60-second demo script was written so a presenter can follow it without preparation.

- **IBM_BOB_USAGE.md** (this document): Bob drafted the initial outline of SDLC phases and fleshed out each with concrete, project-specific examples rather than generic statements like "Bob helped write code."

- **`.env.example`**: Bob enumerated every environment variable referenced anywhere in the codebase (including optional overrides) with one-line comments explaining where to get each key.

- **Inline code comments**: Throughout the codebase, Bob wrote decision-rationale comments at architectural branch points — e.g., why `SIMILARITY_THRESHOLD = 1.4` is used in `rag_engine.py`, why `threading.Lock()` guards writes in `database.py`, why `transcribe_audio()` uses a `finally` block to always delete the temp file.

---

## 7. Code Review and Consistency Checking

After all files were generated, Bob performed a cross-file consistency audit:

- Verified every function called in `app.py` and `dashboard.py` exists in `database.py`, `rag_engine.py`, or `safety_voice.py` with matching names, argument order, and types.
- Confirmed `database.log_query(phone_number, query_text, language, category, is_emergency)` is called with the same five positional arguments in every call site in `app.py`.
- Confirmed `database.fetch_summary_stats()` returns a dict with `total_queries`, `active_languages`, `emergency_count`, and `helpfulness_rate` — all four keys are consumed by `dashboard.py`.
- Confirmed `safety_voice.download_audio(media_url, auth, dest_path)` signature matches all call sites.
- Confirmed `rag_engine.detect_language(text, fallback_language)` default argument matches the two-argument call in `app.py`.
- **Security audit (`security-review.md`)**: Bob diagnosed the root cause of the `ValueError: not enough values to unpack` crash in `fetch_all_logs()` — SQLite's default timestamp converter (`convert_timestamp`) splitting stored values on the byte `b" "` when a `T`-separator or UTC offset was present. Bob applied fixes across `database.py` (all timestamp columns changed from `TIMESTAMP` to `TEXT`; `_get_conn()` deliberately omits `detect_types`), `seed_data.py` (canonical `_fmt()` helper enforcing `YYYY-MM-DD HH:MM:SS`), and `alerting.py` (matching `_fmt()` with a one-time warning on malformed timestamps). Bob then produced a prioritised issue list covering: missing Twilio webhook signature validation (high), single-plaintext-password dashboard auth (high), unbounded Whisper transcription cost (medium), and broad `except Exception` audit guidance (low).

---

## 8. Specific IBM Bob Capabilities Used

| Bob Capability | How it was used in this project |
|---|---|
| Multi-file context window | Held app.py + database.py + dashboard.py in context for cross-cutting changes (feedback loop, flagging, alerts) |
| Code generation with constraints | Generated all stub-free, fully-implemented functions respecting the exact signatures in the spec |
| Architecture reasoning | Argued for the deterministic safety circuit breaker; designed the PII anonymisation contract |
| Iterative refinement | Revised rag_engine.py prompt template after reasoning about multilingual tone requirements |
| Documentation generation | Produced README.md, .env.example, seed_data.py, and this document |
| Consistency audit | Cross-checked function names, signatures, and return types across all six Python source files |
| Scaffolding | Generated Flask+Twilio boilerplate, Streamlit page layout, ChromaDB upsert pattern |
| Soft-fail design | Identified every external I/O call (Whisper, Gemini, ChromaDB) that needed try/except + graceful fallback |
| Startup validation | Added `_verify_twilio_credentials()` and `_verify_google_genai_key()` — real API calls at boot time that surface bad credentials as structured log errors rather than runtime tracebacks |
| Image generation scaffold | Designed `card_generator.py` with Pillow font fallbacks, cross-platform TTF path resolution, and non-fatal integration in `app.py` |
| Security audit | Diagnosed and fixed SQLite timestamp crash; produced `security-review.md` with a prioritised list of production-readiness issues across the full codebase |

---

---

## 9. Reliability and Security Review

After the stretch goals were implemented, Bob performed a reliability and security pass over the complete codebase, documented in `security-review.md`.

**Bug fixed — `ValueError` in `fetch_all_logs()`:**
Bob traced the crash to SQLite's built-in `convert_timestamp` converter, which splits stored values on `b" "`. Any timestamp written with a `T` separator or UTC offset caused `datepart, timepart = val.split(b" ")` to raise `ValueError: not enough values to unpack`. The fix was applied in three files:
- `database.py`: all timestamp columns declared `TEXT`; `_get_conn()` omits `detect_types=sqlite3.PARSE_DECLTYPES` and documents this in its docstring; WAL journal mode added for concurrent read performance.
- `seed_data.py`: all writers use a single `_fmt()` helper producing exactly `YYYY-MM-DD HH:MM:SS`.
- `alerting.py`: matching `_fmt()` helper; one-time warning logged if a `T` is detected in a fresh timestamp.

**Prioritised issue list Bob produced:**

| Priority | Issue | Recommended fix |
|---|---|---|
| High | Twilio webhook has no signature validation | Validate `X-Twilio-Signature` via `RequestValidator`; reject invalid requests with 403 |
| High | Dashboard password is single plaintext string | Acceptable for demo; must be hardened before internet-facing deployment |
| Medium | Whisper transcription is unbounded and unthrottled | Add per-phone cooldown or max-duration cap |
| Medium | `NamedTemporaryFile(delete=False)` leaves orphans on crash | Use a context-managed temp directory purged on startup |
| Low | Broad `except Exception` in webhook | Audit each site; split `sqlite3.Error` from generic exceptions where appropriate |
| Low | `_find_best_fact_record` is a keyword scan, not vector search | Align with ChromaDB similarity search if used for correctness-sensitive paths |
| Low | Missing `CARD_BASE_URL` silently skips card attachment | Log an `INFO` line when the card is skipped due to absent base URL |

Bob also produced a security posture summary confirming: no hardcoded secrets, no `eval`/`os.system`/shell injection path, dashboard `unsafe_allow_html` built from static templates only (low XSS risk), and the privacy contract (raw phone numbers in `sessions` only; SHA-256 truncated hash everywhere else) working as intended.

---

*This document was produced in collaboration with IBM Bob as part of the health-myth-bot IBM Hackathon submission.*
