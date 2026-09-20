"""Core bot logic — deliberately framework-free.

Every module here is plain Python so it can be driven by the WhatsApp webhook
(``app.py``), the website demo (``demo.py``), the Streamlit dashboard, the eval
harness and the test suite without any of them caring how the others work.

Layout
------
* ``language``        — detection + localised strings (en / hi / kn)
* ``retrieval``       — BM25 (+ optional local embeddings) over ``data/health_facts.json``
* ``rag_engine``      — answer generation: retrieval context -> Gemini/Ollama -> fallback
* ``safety_voice``    — deterministic emergency short-circuit
* ``tts``             — edge-tts voice replies
* ``channels``        — WhatsApp Cloud API / Twilio adapters
* ``database``        — sqlite log of every query, answer and feedback event
* ``advisory``        — outbound advisories, only inside a 24-hour service window
* ``alerting``        — rumour-spike detection for the dashboard
* ``card_generator``  — myth-card images (HTML -> headless Chromium)
* ``card_renderer``   — the card templates the generator renders
* ``review_corpus``   — CLI for the human translation-review workflow
                        (run as ``python -m bot.review_corpus``)

Import style: sibling modules use absolute ``from bot import X`` so the package
works from any entry point that has the project root on ``sys.path``.
"""
