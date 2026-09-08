# 🏥 Health Myth-Bot

> **Multilingual WhatsApp bot that busts common health myths using RAG — with a real-time analytics dashboard for public health officials.**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-green)](https://flask.palletsprojects.com)
[![Twilio](https://img.shields.io/badge/Twilio-WhatsApp-red)](https://twilio.com)
[![Gemini](https://img.shields.io/badge/Gemini-3.x-flash-orange)](https://deepmind.google/technologies/gemini/)
[![ChromaDB](https://img.shields.io/badge/ChromaDB-Vector%20Store-purple)](https://www.trychroma.com)
[![IBM Bob](https://img.shields.io/badge/IBM%20Bob-SDLC%20Partner-0062ff)](https://ibm.com)

---

## Architecture

```
WhatsApp User
      │
      │  (sends message / voice note)
      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Twilio WhatsApp Business API                     │
│                    (webhook POST /webhook)                          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          app.py  (Flask)                            │
│                                                                     │
│  1. Audio? ──► safety_voice.download_audio()                        │
│               safety_voice.transcribe_audio()  [Google Gemini STT]  │
│                                                                     │
│  2. ┌─── safety_voice.check_emergency()  ← ALWAYS FIRST             │
│     │    (deterministic keyword matching, NO LLM)                  │
│     │                                                               │
│     │  Emergency ──► log + immediate Twilio reply ──► STOP         │
│     │                                                               │
│     └─── NOT emergency → continue                                   │
│                                                                     │
│  3. Language detection / session lookup  [database.py + langdetect] │
│                                                                     │
│  4. rag_engine.get_myth_rebuttal()                                  │
│       │                                                             │
│       ├─► ChromaDB query  (./chroma_db)                            │
│       │   health_facts.json (8 verified records)                   │
│       │                                                             │
│       └─► Google Gemini API (gemini-3.6-flash)                     │
│           └─► empathetic reply in target language                  │
│                                                                     │
│  5. database.log_query()  [SQLite, anonymised hash]                │
│  6. Feedback prompt  ►  database.record_feedback()                  │
│  7. Flag-for-review path  ►  database.flag_myth()                   │
│  8. Myth card generation  ►  card_generator.generate_myth_card()    │
└─────────────────────────────────────────────────────────────────────┘
                           │
          ┌────────────────┴────────────────┐
          │                                 │
          ▼                                 ▼
 SQLite database                    ChromaDB (./chroma_db)
 (logs, sessions,                   (persistent vector store)
  feedback, alerts,
  flagged_myths)
          │
          ▼
┌────────────────────────────┐
│  dashboard.py  (Streamlit) │
│  - Password gate           │
│  - Summary metric cards    │
│  - Pie chart: by language  │
│  - Bar chart: by category  │
│  - Filter + data table     │
│  - Rumour spike alerts     │
│  - Flagged myths queue     │
│  - CSV / PDF export        │
└────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Messaging channel | Twilio WhatsApp Business API |
| Web framework | Flask 3.x |
| RAG vector store | ChromaDB (persistent local) |
| LLM | Google Gemini 3.6 Flash (`google-genai`) |
| Voice transcription | Google Gemini speech-to-text (`google-genai`) |
| Safety layer | Deterministic Python keyword circuit breaker |
| Persistence | SQLite3 (stdlib — no ORM) |
| Analytics UI | Streamlit + Plotly |
| Language detection | `langdetect` |
| Image cards | Pillow |
| PDF export | fpdf2 |
| SDLC partner | IBM Bob |

---

## Setup

### Prerequisites
- Python 3.11 or higher
- A [Twilio account](https://twilio.com) with WhatsApp sandbox enabled
- [Google AI Studio](https://aistudio.google.com) API key (Gemini, used for both chat and voice transcription)
- A tunnel tool such as [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) (the project ships a local `cloudflared` binary) or ngrok to expose localhost during development

---

### Step 1 — Clone and create a virtual environment

```bash
git clone https://github.com/your-org/health-myth-bot.git
cd health-myth-bot
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

### Step 2 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 3 — Configure environment variables

```bash
cp .env.example .env
# Open .env in your editor and fill in:
#   TWILIO_ACCOUNT_SID
#   TWILIO_AUTH_TOKEN
#   GEMINI_API_KEY
#   DASHBOARD_PASSWORD   (any string you choose)
```

### Step 4 — Seed the database with sample data

```bash
python seed_data.py
```

Expected output:
```
Cleared existing seed rows.
Inserted 30 log rows.
Inserted 10 feedback rows.
Inserted 1 flagged myth row.
Upserted 6 session rows.

✅ Seed complete. Run 'streamlit run dashboard.py' to see live charts.
```

### Step 5 — Start the Flask webhook server

```bash
python app.py
# Server starts on http://0.0.0.0:5000
```

### Step 6 — Start the Streamlit dashboard

Open a **second terminal** (same virtualenv):

```bash
streamlit run dashboard.py
# Opens http://localhost:8501 in your browser
# Enter the DASHBOARD_PASSWORD you set in .env
```

### Step 7 — Expose webhook via cloudflared for Twilio sandbox

Open a **third terminal** and make sure the project-local `cloudflared` binary is executable:

```bash
chmod +x cloudflared
./cloudflared tunnel --url http://localhost:5000
```

`cloudflared` prints a public `https://...trycloudflare.com` URL. Copy that URL, then:

1. Go to [Twilio Console → Messaging → WhatsApp Sandbox](https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn)
2. Set the **"When a message comes in"** webhook URL to:
   `https://<your-random>.trycloudflare.com/webhook`
3. Method: `HTTP POST`
4. Save.

### Step 8 — Join the sandbox

Send the Twilio sandbox join code (e.g., `join bright-horse`) from your WhatsApp to the sandbox number shown in the console. You only need to do this once.

---

## 60-Second Live Demo Script

Send these messages from your WhatsApp to the sandbox number in order:

| # | Message to send | Expected bot behaviour |
|---|---|---|
| 1 | `Does the MMR vaccine cause autism?` | Bot auto-detects English, sets language, returns empathetic myth-bust with WHO citation. Appends 👍/👎 feedback prompt. |
| 2 | `👍` | Bot records positive feedback, thanks you. |
| 3 | `भाषा` | Language-change menu appears (1/2/3 options). |
| 4 | `2` | Language set to Hindi. Confirmation in Hindi. |
| 5 | `क्या एंटीबायोटिक से सर्दी ठीक होती है?` | Antibiotic myth busted in Hindi with citation. |
| 6 | `My husband has severe chest pain and difficulty breathing` | **Emergency path triggers.** Bot immediately returns emergency services message, skips all RAG/LLM processing. Dashboard emergency counter increments. |
| 7 | `Can drinking cow urine cure all diseases?` | No ChromaDB match → fallback + "Would you like to flag this for review?" prompt. |
| 8 | `YES` | Myth flagged to pending review queue, visible in dashboard. |

> **Note on message 6:** The safety check is purely deterministic — it matches "chest pain" and "difficulty breathing" keywords before any LLM call. This ensures zero latency and zero chance of an LLM misclassifying an emergency.

---

## Project Structure

```
health-myth-bot/
├── app.py              Flask webhook + request orchestration
├── rag_engine.py       ChromaDB vector store + Gemini rebuttal
├── safety_voice.py     Emergency circuit breaker + Gemini STT
├── database.py         SQLite schema + all DB functions
├── dashboard.py        Streamlit analytics dashboard
├── seed_data.py        30-row realistic seed data script
├── card_generator.py   Pillow-based myth-fact shareable card
├── alerting.py         Rumour spike detection (stretch goal 4.4)
├── health_facts.json   8 verified myth/fact records (WHO / MoH sourced)
├── static/
│   ├── index.html     Self-contained landing page for the bot (mirror of dashboard styling)
│   └── cards/        Generated myth-fact card PNGs (stretch goal 4.6)
├── requirements.txt
├── .env.example
├── README.md
└── IBM_BOB_USAGE.md
```

---

## Stretch Goals Implemented

| # | Feature | Status |
|---|---|---|
| 4.1 | Feedback loop (👍/👎) | ✅ Fully implemented |
| 4.2 | Auto-detect language | ✅ langdetect, 3-word threshold |
| 4.3 | WhatsApp quick-reply buttons | ⚠️ Flag documented in `.env.example`; interactive-message API not implemented in the demo |
| 4.4 | Proactive rumour-spike alerting | ✅ alerting.detect_rumor_spike() |
| 4.5 | Report a new myth intake | ✅ flagged_myths table + dashboard queue |
| 4.6 | Shareable myth-fact card image | ✅ card_generator.py (Pillow) |
| 4.7 | Dashboard password gate | ✅ DASHBOARD_PASSWORD env var |
| 4.8 | CSV + PDF export | ✅ Filtered CSV download + fpdf2 weekly report |

---

## Known Limitations

1. **Authentication is not production-grade.** The `DASHBOARD_PASSWORD` check is a single plaintext password stored in the `.env` file. A real deployment would use OAuth 2.0 / SSO with role-based access control.

2. **SQLite is not suitable for high concurrency.** Under heavy load (hundreds of simultaneous users), replace SQLite with PostgreSQL. The `database.py` module uses a `threading.Lock` and WAL mode to handle the Flask dev server's thread pool safely, but this is not a substitute for a production database.

3. **ChromaDB runs in-process.** For production, deploy ChromaDB as a standalone server (or replace with a managed vector database) so the embedding index survives restarts and scales independently.

4. **`CARD_BASE_URL` must be set for media cards to attach.** The `generate_myth_card()` function writes PNG files to `./static/cards/`. These must be served from a publicly accessible URL and set via the `CARD_BASE_URL` env var — otherwise the card attachment is silently skipped.

5. **Gemini transcription costs money.** Every voice note incurs a Google Gemini API cost. The current implementation has no rate-limiting or per-user quotas.

6. **`langdetect` is non-deterministic by default.** We seed `DetectorFactory.seed = 42` for reproducibility, but detection accuracy is limited on short messages — this is why messages under 3 words fall back to the stored language or the manual menu.

7. **No webhook signature validation.** In production, validate Twilio's `X-Twilio-Webhook-SignatureV2` header on every request (using your `TWILIO_AUTH_TOKEN`) to prevent spoofed webhooks.

8. **The `SUPPORTS_INTERACTIVE_MESSAGES` flag is documented but the full interactive-message Twilio API is not exercised in the demo.** This requires a WhatsApp Business API account (not sandbox).

---

## License

MIT License — see LICENSE file.
