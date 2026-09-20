# Security & reliability review — health-myth-bot

Last verified against: `app.py`, `channels.py`, `rag_engine.py`, `retrieval.py`,
`safety_voice.py`, `language.py`, `database.py`, `advisory.py`,
`card_renderer.py`, `dashboard.py`, `run_all.bat`.

This review is kept current rather than historical: every item below records its
**status now**, and the two sections at the end list what is still open. The
original trigger for the review is preserved in section 1 because the reasoning
still constrains how timestamps may be written.

---

## 1. Historical issue — ValueError in fetch_all_logs

**Root cause:** SQLite's default timestamp converter splits a stored value on the
byte `b" "`. If any timestamp column contains a string without exactly one space
in that layout (an ISO-8601 string with a `T` separator, a UTC offset, or
microseconds), the converter raises
`ValueError: not enough values to unpack (expected 2, got 1)` even though the
Python code never asked SQLite to convert the column.

**Fixes applied, still in force**

- `database.py`: all timestamp columns are declared `TEXT`, and `_get_conn()`
  deliberately does not set `detect_types=sqlite3.PARSE_DECLTYPES`, so SQLite
  never runs `convert_timestamp` on them. The storage contract is documented in
  the module docstring.
- `seed_data.py` and `database.py` funnel every writer through one `_fmt()` /
  `_utcnow()` helper producing exactly `YYYY-MM-DD HH:MM:SS`.
- `alerting.py` logs a warning if it ever sees a `T` in a fresh timestamp.
- Range queries (`timestamp >= ?`) remain valid because the layout is
  ASCII-sortable.

**Verification:** `database.init_db()`, `database.fetch_all_logs()`,
`database.fetch_summary_stats()` and `alerting.detect_rumor_spike()` all return
cleanly, and `tests/` covers the pending-state and window comparisons that rely
on the same string ordering.

If the crash reappears, the on-disk database contains a row written by an older
writer: re-seed with `python seed_data.py`, or normalise every `timestamp` value
to `%Y-%m-%d %H:%M:%S`.

---

## 2. Findings and their status

### Resolved

- **Webhook signature validation was missing.** *Fixed, and now fails closed.*

  The old handler trusted `From`, `Body`, `MediaUrl0` and `MediaContentType0`
  from anyone who could reach the URL. Both adapters now authenticate first:

  - WhatsApp Cloud API: `X-Hub-Signature-256` = HMAC-SHA256 over the raw body,
    keyed with the Meta app secret, compared with `hmac.compare_digest`.
  - Twilio: `twilio.request_validator.RequestValidator` over the full public URL
    (honouring `X-Forwarded-Host` behind the tunnel) plus the form fields.
  - If the relevant secret is absent the request is **rejected with 403**, not
    accepted. An unauthenticated webhook is an open door, and a silent warning
    would leave it open.
  - `VALIDATE_WEBHOOK_SIGNATURES` exists only so the test suite can turn the
    check off for non-security tests; it logs a loud warning whenever it is off.
  - Covered by tests: valid signature accepted, unsigned rejected, body tampered
    after signing rejected, missing secret rejects everything.

- **Every voice note hit a paid API with no throttle or cap.** *Fixed.*

  Speech-to-text now runs locally first (`faster-whisper`, then `openai-whisper`)
  and only falls back to Gemini. No per-minute cost, so the cost guard is no
  longer the only thing standing between a demo and a bill. A per-user rate
  limit (`RATE_LIMIT_PER_HOUR`, default 20) and an audio size cap
  (`MAX_AUDIO_BYTES`, 16 MB) also apply.

- **Temp audio files could leak.** *Fixed.*

  The file is created before the download and deleted on every failure path, and
  `transcribe_audio()` deletes it in a `finally`. A test asserts that a failed
  download leaves no file behind.

- **`_find_best_fact_record` was a substring keyword scan.** *Fixed.*

  It now delegates to `retrieval.py`, the same index the answer is grounded in,
  with explicit blocking of the substring bug: Latin keywords must match whole
  tokens. The regression this caused — the token `in` (from *"Tetanus Toxoid **in**
  Pregnancy"*) attaching the tetanus card to "online order in transit" — has its
  own test.

- **Cards silently disappeared when `CARD_BASE_URL` was unset.** *Fixed.*

  The Cloud API channel uploads the image and references it by media id, so no
  public URL is needed at all. `CARD_BASE_URL` is only used by the legacy Twilio
  adapter, where a missing value is logged.

- **Prompt-injection surface in the answer path.** *Reduced.*

  User text is interpolated into the prompt, so it can attempt instruction
  override. Two mitigations now exist: a scope instruction limiting the bot to
  health questions, and grounding strictly in retrieved corpus text. This is
  mitigation, not immunity — see section 3.

- **Untracked operational risk: scheduled outbound messaging was free-form.**
  *Fixed by construction.* `advisory.py` sends only to users inside their open
  24-hour window and records a skip count for everyone else, so a proactive
  campaign cannot quietly become billable, and a spike cannot re-message the
  same category inside the cooldown.

### Open

- **Dashboard auth is still a single plaintext password.** `DASHBOARD_PASSWORD`
  is compared in memory. Acceptable for a pilot; if the dashboard is reachable
  through the tunnel, treat it as effectively open and add OAuth/SSO with roles
  before sharing the URL.

- **Broad `except Exception` remains throughout.** Intentional at the webhook
  boundary — a 200 with a friendly message beats a 500 retry storm — but each
  site was re-checked this pass: the handlers log the exception with
  `logger.exception`/`logger.error` and degrade to a user-visible fallback
  message rather than silently returning wrong data. The highest-risk one (a
  failed send being invisible to everyone) is now logged as an error on the
  delivery path.

- **No per-IP throttling.** The rate limit is per phone number, which is the
  right unit for this bot, but it does not stop someone signing up many numbers.

---

## 3. What cannot be fully solved here

- **Prompt injection and model fabrication.** Grounding plus "do not invent
  statistics" reduces fabrication; it cannot eliminate it. The corpus and its
  citations remain the reviewable artifact, and `eval/` is the regression gate
  on retrieval, so a fabricated answer is at least visible as a retrieval
  failure rather than an invisible one.
- **Free-tier data handling.** On Gemini's free tier, Google may use submitted
  content to improve its products. For genuine patient questions, run Ollama
  locally — the free choice that also keeps data on the machine. This is
  documented in the README and `.env.example` rather than left implicit.
- **Translation correctness.** Machine-assisted translations of clinical
  guidance are treated as drafts that nobody has read. They are not used as
  source text and are not printed on cards until a named reviewer approves them
  (`bot/review_corpus.py`). This is a process control, not a technical guarantee.

---

## 4. Security posture summary

- **Secrets:** loaded from environment variables; none hardcoded. `.env` is
  gitignored. WhatsApp is a bearer token over HTTPS; Twilio credentials are used
  only for API auth and media fetch.
- **Input safety:** no `eval`, no `os.system`, no shell interpolation. All
  `subprocess` calls (Chromium, ffmpeg) use a fixed argv list with no `shell=True`
  and no user-controlled arguments. Temp file paths are derived from
  `tempfile`, never from user input. Chromium runs with a throwaway
  `--user-data-dir`, so it never touches the operator's real browser profile.
- **Output safety:** the dashboard injects HTML only from static templates and
  controlled colour constants. User text is rendered through `st.dataframe` and
  `st.write`. Card rendering HTML-escapes every interpolated field
  (`html.escape`), so a malicious query cannot inject markup into a card.
- **Emergency path integrity:** `check_emergency()` is pure keyword matching
  with no network, no LLM and no database dependency, and it is checked before
  the kill switch, the rate limit, and any API call — so it works when
  everything else is broken.
- **Privacy:** raw phone numbers live only in `sessions` and
  `service_windows`; `logs`, `feedback` and `flagged_myths` store a truncated
  SHA-256 hash. The dashboard never displays raw numbers, and log lines mask
  them as `+91…01` rather than truncating the front (which produced `whatsa…`
  for every Twilio number).
- **Cost containment:** every outbound message is a reply inside an open
  service window. There is no code path that sends a billable template.

---

## 5. Recommended next actions

1. Add OAuth/SSO with roles to the dashboard before it is exposed beyond
   localhost.
2. Run a local multilingual embedding model (`python retrieval.py
   --download-model`) and re-run `eval/run_eval.py` to calibrate
   `RETRIEVAL_DENSE_FLOOR`, which is currently marked uncalibrated.
3. Have a native speaker review and approve the Hindi and Kannada translations
   (`python -m bot.review_corpus status`) — the corpus currently ships them as
   drafts, and approved text is both more accurate and what makes cards
   bilingual.
4. Grow the corpus beyond 8 facts; retrieval quality tracks corpus size, and
   `eval/` will show whether each addition helps.
