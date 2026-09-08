# Security & reliability review — health-myth-bot

Last verified against HEAD: database.py, seed_data.py, alerting.py, run_all.ps1,
app.py, safety_voice.py, card_generator.py, rag_engine.py, dashboard.py.

## 1. Issue that triggered this run — ValueError in fetch_all_logs
**Root cause:** SQLite's default timestamp converter splits a stored value on the
byte `b" "`.  If any timestamp column ever contains a string that does not have
exactly one space in that layout (for example an ISO-8601 string with a `T`
separator, a UTC offset, or microseconds), the converter raises
`ValueError: not enough values to unpack (expected 2, got 1)` even though the
Python code never asked SQLite to convert the column.

**Why it showed up now:** the stacktrace comes from
`sqlite3/dbapi2.py:convert_timestamp` → `datepart, timepart = val.split(b" ")`.
That path is only entered when SQLite is configured to convert TIMESTAMP columns,
or when a stored value is not a clean `YYYY-MM-DD HH:MM:SS` string.

**Fixes applied**
- `database.py`: all timestamp columns are now declared `TEXT`, not `TIMESTAMP`.
  The `_get_conn()` helper deliberately does **not** set
  `detect_types=sqlite3.PARSE_DECLTYPES`, so SQLite never runs
  `convert_timestamp` on these columns.  The docstring now documents the
  storage contract explicitly.
- `seed_data.py`: all writers use one `_fmt()` helper that produces exactly
  `YYYY-MM-DD HH:MM:SS` with a space separator and no offset.  Session upsert
  rows now print a warning if the stored timestamp differs from what we wrote —
  this catches accidental drift early.
- `alerting.py`: the internal `_fmt()` helper is the canonical form for range
  queries.  The function now logs a one-time warning if it ever sees a `T` in a
  fresh timestamp, which is a cheap signal that something upstream wrote the
  wrong format.  The docstring documents the TEXT-range-query contract.
- `run_all.ps1`: **not** a code defect.  The PowerShell launcher had no
  documentation headers; it now has a clear preamble explaining prerequisites,
  the six startup steps, how to stop the stack, and common troubleshooting
  steps.

**Verification**
- `python -c "import database; database.init_db(); print(database.fetch_all_logs(limit=5))"` — succeeds and returns strings, not converted datetimes.
- `python -c "import seed_data; seed_data.run_seed()"` — completes cleanly after the emoji print was replaced with plain ASCII.
- `python -c "import alerting; alerting.detect_rumor_spike()"` — returns `[]`, no crash.

If the crash still reproduces on your machine after these changes, it means the
**existing database file on disk** already contains a malformed timestamp (for
example an old row written before this fix, or a row from a different writer).
In that case, the fastest recovery is to delete the DB and re-run
`python seed_data.py`, or — if you need to keep live data — to run a one-off
rewriter that normalises every `timestamp` value to `%Y-%m-%d %H:%M:%S`.

## 2. Codebase issues found and their status
These are real issues, not theoretical.  None of them are currently breaking the
app, but several are worth addressing before you expose the bot to real users.

### High priority (fix before production exposure)
- **Webhook signature validation is missing.** `app.py` trusts Twilio's
  `From`, `Body`, `MediaUrl0`, and `MediaContentType0` fields without checking
  `X-Twilio-Signature`.  Anyone who discovers the webhook URL can post fake
  messages that look like real users.  This is the single biggest gap.
  Fix: validate the signature on every `POST /webhook` using
  `twilio.request_validator.RequestValidator` and your auth token.  Reject
  invalid requests with 403.
- **Dashboard auth is a single plaintext password.** `dashboard.py` compares
  `st.text_input(...)` against `os.environ["DASHBOARD_PASSWORD"]` in memory.
  It is fine for a demo, but the README already documents this limitation.  If
  this dashboard is internet-facing through the Cloudflare tunnel, treat it as
  effectively open until you add real auth.

### Medium priority (fix soon)
- **Whisper transcriptions are unbounded and unthrottled.** Every voice note
  hits the OpenAI API with no per-user rate limit, no length cap, and no cost
  guard.  In a busy deployment this can become expensive quickly.  Add a simple
  cap: max file duration, or max characters of transcript, or a per-phone cooldown.
- **`tempfile.NamedTemporaryFile(delete=False)` is used for audio.** The
  `app.py` audio path writes to a predictable temp name and relies on the
  `finally` block in `safety_voice.transcribe_audio()` to clean up.  That is
  acceptable for a demo, but if the process crashes between write and
  transcription, orphaned temp files accumulate.  Prefer a context manager that
  always cleans up, or rotate through a small temp directory that you purge on
  startup.

### Low priority (defensibility improvements)
- **Broad `except Exception` in many places.** This is intentional for the
  Twilio webhook — a 200 with a friendly error message is better than a 500
  retry storm.  It is still worth auditing each site to make sure the caught
  exception cannot mask a bug you actually want to see in logs.  The recent
  changes already split `sqlite3.Error` from generic exceptions in `alerting.py`,
  which is the right direction.
- **`_find_best_fact_record` in `app.py` is a simple keyword scan.** It is fine
  for the card-generation stretch goal, but it is not the same as the ChromaDB
  similarity search.  If you rely on it for anything correctness-sensitive,
  align it with the vector store.
- **Static asset URL construction in `app.py`.** The card attachment logic
  builds a URL from `CARD_BASE_URL` + filename.  If `CARD_BASE_URL` is missing,
  the card is silently skipped.  That is documented in the README and in the
  code, so it is not a bug, but it is easy for an operator to misconfigure and
  then wonder why cards are missing.  Consider logging an INFO line when the
  card is skipped because the base URL is absent.

## 3. Security posture summary
- **Secrets handling:** API keys are loaded from env vars at import time and
  validated before the app starts.  No secrets are hardcoded.  Twilio auth is
  passed to `requests` only over HTTPS via the Twilio media URL.  This is good.
- **Input safety:** user text flows through deterministic keyword matching,
  ChromaDB, and Gemini.  There is no `eval`, no `os.system`, no shell injection
  path in the current code.  Temp file paths are not derived from user input.
- **Output safety:** the dashboard renders user-generated query text via
  `st.dataframe` and `unsafe_allow_html` CSS only.  The HTML injected through
  `unsafe_allow_html` is built from static templates and controlled colour
  constants, not from user input, so the current XSS risk is low.  Watch this
  if you ever start rendering user-supplied HTML.
- **Privacy:** raw phone numbers are stored only in the `sessions` table, and
  the dashboard intentionally never displays them.  The `logs` and
  `flagged_myths` tables store an SHA-256 truncated hash.  This is the right
  design and it is working as intended.

## 4. Recommended next actions
1. Add Twilio signature validation to `POST /webhook` before going live.
2. Confirm the existing local `.db` file does not already contain a malformed
   timestamp row; if it does, re-seed or patch it.
3. Decide whether the dashboard will ever be reachable from outside your machine.
   If yes, harden auth before sharing the tunnel URL.
4. Add a simple Whisper cost/guard rail before you field-test voice notes with
   real users.
