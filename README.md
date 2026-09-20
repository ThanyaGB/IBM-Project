# 🏥 Health Myth-Bot

> **Multilingual WhatsApp bot that busts common health myths using RAG — with a real-time analytics dashboard for public health officials. Runs entirely on free infrastructure.**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-green)](https://flask.palletsprojects.com)
[![WhatsApp Cloud API](https://img.shields.io/badge/WhatsApp-Cloud%20API-25D366)](https://developers.facebook.com/docs/whatsapp)
[![Gemini](https://img.shields.io/badge/Gemini-free%20tier-orange)](https://ai.google.dev)
[![Tests](https://img.shields.io/badge/tests-191%20passing-brightgreen)](#testing)
[![IBM Bob](https://img.shields.io/badge/IBM%20Bob-SDLC%20Partner-0062ff)](https://ibm.com)

---

## Cost model: why this is free

There is no paid credential anywhere in this project, and the architecture is what makes that true rather than a budget that has to be watched.

| Component | Choice | Why it costs nothing |
|---|---|---|
| Messaging | **WhatsApp Cloud API** (official, direct) | Inbound messages are never charged. A *non-template* reply — text, image, audio, buttons — is free while the 24-hour customer-service window opened by the user's own message is open. The bot only ever replies, so it never sends a billable template. |
| Answer generation | Gemini **free tier**, or **Ollama** locally | Ollama costs nothing and keeps patient data on the machine. |
| Retrieval | **In-process BM25 + optional local embeddings** | No hosted vector database, no embedding API. |
| Speech-to-text | **faster-whisper** locally (Gemini as fallback) | No per-minute transcription charge. |
| Text-to-speech | **edge-tts** | Free neural Hindi and Kannada voices, no API key. |
| Card rendering | **Headless Chromium** already on the machine | No rendering service. |
| Hosting | Local + a free `cloudflared` tunnel for the webhook | Fine for a pilot; the demo never needs a paid host. |

Two consequences worth knowing:

- **Twilio WhatsApp was removed from the default path.** It charges per message in both directions. `CHANNEL=twilio` still exists as an adapter for existing deployments, but it is not the default and nothing else depends on it.
- **`advisory.py` cannot overspend.** Proactive messages are sent *only* to users inside their open 24-hour window, which is exactly the set of recipients Meta does not charge for. Anyone outside it is skipped and counted, and the skip count is recorded rather than hidden.

---

## Architecture

```
WhatsApp User
      │  (text / voice note)
      ▼
┌──────────────────────────────────────────────────────────────────────┐
│  WhatsApp Cloud API  ──POST /webhook──►  bot/channels.py             │
│  · X-Hub-Signature-256 HMAC validation (fails closed)                │
│  · normalises payloads into InboundMessage / OutboundReply            │
│    (Twilio is an alternative adapter behind CHANNEL=twilio)           │
└──────────────────────────┬───────────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  app.py  (Flask) — one core, any channel                             │
│                                                                      ││  1. Open the user's 24h free reply window                            │
│  2. safety_voice.check_emergency()   ← ALWAYS FIRST, no LLM          │
│       word-boundary matching over en / hi / kn (native + romanised)  │
│       hit ──► log + emergency reply in the user's language ──► STOP  │
│  3. Kill switch (env or dashboard)  and  per-user rate limit         │
│  4. Voice note? ──► download ──► faster-whisper (local) ──► text     │
│  5. Language: stored ─► detect (script + romanised lexicon) ─► menu  │
│  6. rag_engine.get_myth_rebuttal()                                   │
│       bot/retrieval.py   BM25 + optional local multilingual embeddings│
│       data/health_facts.json  8 source-attributed records, 3 langs   │
│       └─► Gemini free tier  or  Ollama (local)  ──► localised reply  │
│  7. database.log_query()   SQLite, anonymised hash                   │
│  8. Reply assembly: card image, feedback buttons, spoken answer      │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
          ┌────────────────┴────────────────┐
          ▼                                 ▼
  SQLite (logs, sessions,                                data/health_facts.json
   feedback, alerts, flagged_myths,   (+ review metadata
   processed_messages, pending_state,  per translation)
   service_windows, settings)
          │
          ▼
┌──────────────────────────────────────────┐
│  dashboard.py  (Streamlit)               │
│  · Password gate                         │
│  · Metrics, language + category charts   │
│  · Rumour-spike banner + advisory send   │
│  · Kill switch and system status         │
│  · Flagged-myth review queue             │
│  · Translation review status             │
│  · CSV / PDF export                      │
└──────────────────────────────────────────┘
```

---

## Languages

**English, Hindi (हिन्दी) and Kannada (ಕನ್ನಡ)** — Kannada replaced Swahili, which never matched the pilot population.

Two things are handled that the previous implementation was not:

- **Native script and romanised input.** `Hinglish` and `Kanglish` (`mujhe bukhar hai`, `nange jvara ide`) are detected by a lexicon scorer, because `langdetect` only works on native script. Romanised input still receives a reply in the native script, so it is readable and shareable.
- **Refusal to guess.** One or two ambiguous Latin words fall back to the language menu rather than a guess. A wrong guess sends the user a reply they cannot read, which is worse than asking.

---

## Translation review governance

`data/health_facts.json` carries Hindi and Kannada translations of `common_myth` and `verified_fact`. These are **not UI copy** — they are source-attributed clinical guidance about vaccines and HIV. A machine-assisted translation that nobody has read must never become the bot's authority, so:

- A translation is used as source text **only** once a human approves it.
- Until then the English source is used and the model is instructed to translate faithfully and add nothing. The reply is still in the user's language.
- Card images follow the same rule: a draft translation is never printed on a card.
- The dashboard shows each record's status per language, and approval is a CLI action with a named reviewer:

```bash
python -m bot.review_corpus status
python -m bot.review_corpus show 3 kn
python -m bot.review_corpus approve 3 kn --reviewer "Dr A. Rao"
```

---

## Setup

### Prerequisites

- Python 3.11+
- A **Meta developer account** with a WhatsApp **development-mode test number** — free, no billing, and it can message up to 5 verified recipients, which is plenty for a demo
- Optionally [Ollama](https://ollama.com) (`ollama pull llama3.2`) if you want fully local answers with no data leaving the machine
- A free tunnel such as [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) so Meta can reach your webhook

### Step 1 — Clone and create a virtual environment

```bash
git clone https://github.com/your-org/health-myth-bot.git
cd health-myth-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### Step 2 — Install dependencies

```bash
pip install -r requirements.txt
```

Two optional extras unlock capabilities at no cost (see `requirements.txt` for detail):

```bash
pip install fastembed        # local multilingual embeddings for dense retrieval
python -m bot.retrieval --download-model

pip install edge-tts faster-whisper   # voice replies + local speech-to-text
```

### Step 3 — Configure environment variables

```bash
cp .env.example .env
# Fill in:  WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID,
#           WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN,
#           GEMINI_API_KEY (or OLLAMA_BASE_URL), DASHBOARD_PASSWORD
```

`WHATSAPP_APP_SECRET` is not optional in practice: the webhook **rejects unsigned requests**, because an unauthenticated webhook lets anyone who learns the URL inject messages.

### Step 4 — Check the setup before wiring anything up

```bash
python -m pytest tests -q          # 191 tests, fully offline
python eval/run_eval.py            # retrieval / refusal / language / emergency metrics
python seed_data.py                # sample data for the dashboard
```

### Step 5 — Start the server and the dashboard

```bash
python app.py                      # http://0.0.0.0:5000 — serves the landing page at /
streamlit run dashboard.py         # second terminal
```

`GET /health` reports what is configured, what is degraded, and any boot warnings — including whether card rendering can shape Indic scripts and whether an answer backend is reachable.

### Step 6 — Point WhatsApp at it

```bash
./cloudflared tunnel --url http://localhost:5000
```

In the Meta app dashboard → WhatsApp → Configuration:

| Field | Value |
|---|---|
| Callback URL | `https://<your-random>.trycloudflare.com/webhook` |
| Verify token | the `WHATSAPP_VERIFY_TOKEN` you chose |
| Webhook fields | `messages` |

Meta performs a `GET /webhook` handshake immediately — the app answers it and validates every later POST against the app secret.

---

## 60-Second Live Demo Script

| # | Message to send | Expected behaviour |
|---|---|---|
| 1 | `Does the MMR vaccine cause autism?` | English detected, answer grounded in the WHO/CDC fact, shareable card attached, 👍/👎 buttons appear |
| 2 | Tap 👍 | Positive feedback recorded, no typing needed |
| 3 | `भाषा` | Language menu: `1 – English / 2 – हिन्दी / 3 – ಕನ್ನಡ` |
| 4 | `3` | Confirmation in Kannada |
| 5 | `ನನ್ನ ಮಗುವಿಗೆ ತುಂಬಾ ಜ್ವರ ಇದೆ` | Kannada detected, **Fever Management** card — *not* the emergency message, which is what a naive keyword matcher does here |
| 6 | `nange jvara ide, enu madali` | Romanised Kanglish detected, answered in Kannada script |
| 7 | `ನನ್ನ ಅಪ್ಪನ ಎದೆಯಲ್ಲಿ ತುಂಬಾ ನೋವು ಇದೆ` | **Emergency path.** Deterministic, zero LLM calls, reply in Kannada. Dashboard emergency counter increments |
| 8 | `what is the benefit of walking` | Ordinary answer — *not* an emergency. (`fit` used to match inside `benefit` and hijack this) |
| 9 | `Can drinking cow urine cure all diseases?` | No verified answer → fallback + "flag for review?" with buttons |
| 10 | Tap *Yes, report it* | Appears in the dashboard review queue |
| 11 | Dashboard → Operations → **Pause bot** | Next message gets the maintenance notice; the kill switch stops LLM spend immediately |

Send a **voice note** at any point: it is transcribed locally, answered, and the answer comes back as audio.

---

## Website & live demo

The Flask server also serves a small public website:

| URL | What it is |
|---|---|
| `/` | Landing page — the pitch, how it works, myth examples, trust notes. No emulator. |
| `/demo` | Standalone live-demo screen — a WhatsApp-style phone emulator driven by the **real** bot pipeline (RAG + Gemini + cards + emergency short-circuit), with quick-ask chips and 👍/👎 buttons. |
| `/demo/config` | JSON the demo pages read: dashboard URL + whether the bot's kill switch is up. |
| `POST /demo/message` | The emulator's backend — same `_handle_inbound` core the webhook uses, with a 300-char cap, per-session and hourly caps, and an opt-in "mirror in the live dashboard" toggle that writes to the real database. |

The demo is **independent of the running bot**: it needs no WhatsApp credentials, works while the kill switch is on, and never sends anything through the channel adapters. Its "mirror" toggle is the only way demo traffic reaches the dashboard.

The dashboard (`http://localhost:8501`, password-gated) is linked from both the landing page and the demo screen.

---

## Project structure

```
health-myth-bot/
├── app.py              Flask core: webhook, landing page, kill switch, reply assembly
├── demo.py             Website demo: /, /demo, /demo/config, /demo/message
├── dashboard.py        Streamlit analytics + operations + review queues
├── seed_data.py        Realistic seed data across en/hi/kn
├── bot/                Core logic — framework-free, shared by every entry point
│   ├── language.py     Supported languages, every user-facing string, detection
│   ├── retrieval.py    BM25 + optional local embeddings, corpus loading, review status
│   ├── rag_engine.py   Grounded generation, Gemini free tier / Ollama, citations
│   ├── safety_voice.py Emergency circuit breaker + layered local STT
│   ├── card_renderer.py HTML → PNG via headless Chromium (correct Indic shaping)
│   ├── card_generator.py Card entry point; Pillow fallback (English only)
│   ├── tts.py          Voice replies (edge-tts → OGG/Opus)
│   ├── channels.py     WhatsApp Cloud API + Twilio adapters, signature validation
│   ├── advisory.py     Proactive advisories, gated by the free 24h window
│   ├── alerting.py     Rumour-spike detection
│   ├── database.py     SQLite schema + all queries, pending state, service windows
│   └── review_corpus.py Translation review gate (approve/reject with a reviewer)
├── data/
│   ├── health_facts.json   8 source-attributed records, 3 languages, review metadata
│   └── health_myth_bot.db  SQLite (created at first run; gitignored)
├── static/
│   ├── index.html      Landing page (dark theme)
│   ├── demo.html       Standalone live-demo screen
│   └── cards/          Generated myth-card images (gitignored)
├── tests/              pytest suite (191 tests, offline)
├── eval/               Labelled eval set + metrics gate
├── docs/               security-review.md, ibm-bob-usage.md
├── .github/workflows/  CI: compile, tests, eval gate
├── requirements.txt
└── .env.example
```

---

## Testing

```bash
python -m pytest tests -q      # 191 tests, no network, no credentials
python eval/run_eval.py -v     # metrics with a pass/fail gate for CI
```

Coverage focuses on the ways this bot can hurt someone:

- **Emergency precision and recall** — every phrase that fires, and the exact false positives that used to: `benefit of walking`, `fitness`, mild headache, `in transit`.
- **Retrieval accuracy** — 100% on the labelled set across English, Devanagari, Kannada and romanised input.
- **Refusal** — off-topic queries get "no verified answer" instead of a confident wrong card.
- **Language detection** — including romanised input and the short-message fallback.
- **The free-tier guarantee** — advisories reach only users inside their window, and the kill switch is checked before any LLM call.
- **Webhook security** — valid signature accepted, unsigned and tampered bodies rejected, missing secret fails closed.
- **Governance** — draft translations are never used as source text or printed on a card.

---

## Known limitations

1. **Dashboard authentication is a single shared password.** Fine for a pilot, not for a real deployment — that needs OAuth/SSO with roles.
2. **SQLite is not built for high concurrency.** WAL mode plus a write lock handles a demo's traffic safely; a real pilot needs PostgreSQL.
3. **Free-tier quotas are real.** Gemini's free tier has a requests-per-minute and per-day ceiling, and on the free tier Google may use submitted content to improve its products. For patient data, run Ollama locally. The per-user rate limit exists to keep a single user from exhausting the daily quota.
4. **Wall-clock ceiling for outbound mail.** WhatsApp's window logic lets advisories reach only users who messaged recently; a user who has been quiet for a day cannot be reached for free. That is the trade-off the design accepts deliberately.
5. **Dense retrieval is opt-in.** Without the local embedding model, retrieval is lexical-only — strong on keyword-bearing questions, weaker on pure paraphrase. The eval set measures the difference.
6. **Voice replies need `ffmpeg`.** WhatsApp accepts only `audio/ogg` with Opus, so without ffmpeg the bot sends text instead of silently failing.
7. **Card images need Chrome or Edge.** Without a Chromium-family browser the Pillow fallback renders English only, because Pillow cannot shape Devanagari or Kannada without Raqm.
8. **The corpus is 8 facts.** Small by design for a hackathon; retrieval quality will follow corpus size, and `eval/` is how that gets measured rather than assumed.

---

## License

MIT License — see LICENSE file.
