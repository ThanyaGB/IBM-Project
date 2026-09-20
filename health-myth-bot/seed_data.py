"""Populate the database with sample log rows.

Usage:
    python seed_data.py

Behaviour:
- Calls database.init_db() to ensure tables exist.
- Clears existing seed rows first (idempotent re-seed).
- Inserts rows spanning English, Hindi, and Kannada — plus one romanised
  Hinglish and one romanised Kanglish row, because those are the inputs the
  language detector has to handle without a native script to go on — across
  all categories, timestamps spread over the last 7 days, with at least 3
  emergency rows.
- Also inserts sample feedback, a flagged myth, and session rows.
- Seeds the 24-hour service window for the demo numbers, so a dashboard-triggered
  advisory can actually be delivered in a demo without inventing recipients.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from bot import database

SEED_MARKER = "[SEED]"

SEED_ROWS = [
    ("Does the MMR vaccine really cause autism? My neighbour says so " + SEED_MARKER, "en", "Vaccines", False, 168),
    ("Is the polio vaccine safe for my 2-year-old daughter? " + SEED_MARKER, "en", "Vaccines", False, 150),
    ("I heard the COVID vaccine has microchips in it, is that true? " + SEED_MARKER, "en", "Vaccines", False, 120),
    ("Why do they give tetanus injection during pregnancy? " + SEED_MARKER, "en", "Maternal Health", False, 96),
    ("My baby has a high fever after vaccination, what should I do? " + SEED_MARKER, "en", "Child Health", False, 90),
    ("Should I wrap my child in a blanket when they have fever? " + SEED_MARKER, "en", "Child Health", False, 72),
    ("Can I give my child antibiotics for a cold and stop when they feel better? " + SEED_MARKER, "en", "Medication", False, 60),
    ("My son is having seizures, he is unconscious " + SEED_MARKER, "en", "Emergency", True, 55),
    ("Is it true antibiotics cure the flu? " + SEED_MARKER, "en", "Medication", False, 48),
    ("My child is not breathing properly after eating something " + SEED_MARKER, "en", "Emergency", True, 44),
    ("क्या एमएमआर वैक्सीन से ऑटिज़्म होता है? " + SEED_MARKER, "hi", "Vaccines", False, 148),
    ("पोलियो की दवा से बच्चे को नुकसान होता है क्या? " + SEED_MARKER, "hi", "Vaccines", False, 132),
    ("गर्भावस्था में टेटनस का इंजेक्शन सुरक्षित है क्या? " + SEED_MARKER, "hi", "Maternal Health", False, 115),
    ("प्रेगनेंसी में क्या खाना चाहिए और क्या नहीं? " + SEED_MARKER, "hi", "Maternal Health", False, 100),
    ("बुखार में बच्चे को कम्बल में लपेटना चाहिए? " + SEED_MARKER, "hi", "Child Health", False, 84),
    ("एंटीबायोटिक से जुकाम ठीक हो जाता है क्या? " + SEED_MARKER, "hi", "Medication", False, 78),
    ("मेरे पति को सीने में दर्द हो रहा है बहुत तेज " + SEED_MARKER, "hi", "Emergency", True, 65),
    ("बच्चे को बुखार है 104 डिग्री, क्या करें? " + SEED_MARKER, "hi", "Child Health", False, 50),
    ("क्या COVID वैक्सीन से DNA बदल जाता है? " + SEED_MARKER, "hi", "Vaccines", False, 36),
    ("दूध पिलाने वाली माँ HIV पॉजिटिव है, क्या बच्चे को दूध पिला सकती है? " + SEED_MARKER, "hi", "Maternal Health", False, 24),
    ("ಎಂಎಂಆರ್ ಲಸಿಕೆಯಿಂದ ಮಕ್ಕಳಿಗೆ ಆಟಿಸಂ ಬರುತ್ತದೆ ಎಂದು ನೆರೆಹೊರೆಯವರು ಹೇಳುತ್ತಾರೆ " + SEED_MARKER, "kn", "Vaccines", False, 160),
    ("ಪೋಲಿಯೊ ಹನಿಗಳಿಂದ ಮಕ್ಕಳಿಗೆ ಯಾವುದಾದರೂ ತೊಂದರೆ ಆಗುತ್ತದೆಯೇ? " + SEED_MARKER, "kn", "Vaccines", False, 140),
    ("ಗರ್ಭಾವಸ್ಥೆಯಲ್ಲಿ ಟೆಟನಸ್ ಚುಚ್ಚುಮದ್ದು ಸುರಕ್ಷಿತವೇ? " + SEED_MARKER, "kn", "Maternal Health", False, 110),
    ("ಗರ್ಭಿಣಿಗೆ ಯಾವ ಆಹಾರ ಕೊಡಬೇಕು, ಯಾವುದು ಕೊಡಬಾರದು? " + SEED_MARKER, "kn", "Maternal Health", False, 88),
    ("ಜ್ವರ ಇರುವ ಮಗುವನ್ನು ಕಂಬಳಿಯಲ್ಲಿ ಸುತ್ತಬೇಕೇ? " + SEED_MARKER, "kn", "Child Health", False, 76),
    ("ಆಂಟಿಬಯಾಟಿಕ್ನಿಂದ ಶೀತ ವಾಸಿಯಾಗುತ್ತದೆಯೇ? " + SEED_MARKER, "kn", "Medication", False, 66),
    ("ಕೋವಿಡ್ ಲಸಿಕೆಯಿಂದ ಡಿಎನ್‌ಎ ಬದಲಾಗುತ್ತದೆಯೇ? " + SEED_MARKER, "kn", "Vaccines", False, 54),
    ("ನನ್ನ ಅಪ್ಪನ ಎದೆಯಲ್ಲಿ ತುಂಬಾ ನೋವು ಇದೆ, ಈಗಲೇ ಏನು ಮಾಡಬೇಕು " + SEED_MARKER, "kn", "Emergency", True, 42),
    ("ನನ್ನ ಮಗು ತುಂಬಾ ಔಷಧಿ ನುಂಗಿದೆ, ಸುಸ್ತಾಗಿದೆ " + SEED_MARKER, "kn", "Medication", False, 30),
    ("ಎಚ್‌ಐವಿ ಇರುವ ತಾಯಿ ಮಗುವಿಗೆ ಎದೆಹಾಲು ಕೊಡಬಹುದೇ? " + SEED_MARKER, "kn", "Maternal Health", False, 12),
    # Romanised input: no native script for langdetect to work with, which is
    # exactly why the detector has its own romanised lexicon scorer.
    ("nange tumba jvara ide, enu madali " + SEED_MARKER, "kn", "Child Health", False, 8),
    ("mujhe bukhar aur khaansi hai, kaun si dawa lein " + SEED_MARKER, "hi", "Child Health", False, 6),
]

assert len(SEED_ROWS) == 32
assert sum(1 for r in SEED_ROWS if r[3]) >= 3
assert {r[1] for r in SEED_ROWS} == {"en", "hi", "kn"}


def _fmt(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


def run_seed() -> None:
    database.init_db()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        conn.execute(f"DELETE FROM logs WHERE query_text LIKE '%{SEED_MARKER}%'")
        conn.execute(f"DELETE FROM flagged_myths WHERE query_text LIKE '%{SEED_MARKER}%'")
        conn.execute("DELETE FROM feedback WHERE log_id NOT IN (SELECT id FROM logs)")
        conn.commit()
        print("Cleared existing seed rows.")

        demo_phones = [
            "+91XXXXXXXXX1", "+91XXXXXXXXX2", "+91XXXXXXXXX3",
            "+91XXXXXXXXX4", "+91XXXXXXXXX5", "+1XXXXXXXXXX6",
        ]

        inserted_ids = []
        for i, (query, lang, cat, is_emerg, delta_h) in enumerate(SEED_ROWS):
            phone = demo_phones[i % len(demo_phones)]
            anon = _hash_phone(phone)
            ts = _fmt(now - timedelta(hours=delta_h))
            cursor = conn.execute(
                """
                INSERT INTO logs
                    (anonymized_hash, query_text, language, category, is_emergency, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (anon, query, lang, cat, int(is_emerg), ts),
            )
            inserted_ids.append(cursor.lastrowid)

        conn.commit()
        print(f"Inserted {len(SEED_ROWS)} log rows.")

        non_emerg_ids = [
            inserted_ids[i]
            for i, (_, _, _, is_emerg, _) in enumerate(SEED_ROWS)
            if not is_emerg
        ][:10]

        for j, lid in enumerate(non_emerg_ids):
            rating = 1 if j < 7 else -1
            fb_ts = _fmt(now - timedelta(hours=j * 2))
            conn.execute(
                "INSERT INTO feedback (log_id, rating, timestamp) VALUES (?, ?, ?)",
                (lid, rating, fb_ts),
            )
        conn.commit()
        print(f"Inserted {len(non_emerg_ids)} feedback rows.")

        conn.execute(
            """
            INSERT INTO flagged_myths (query_text, language, timestamp, status)
            VALUES (?, ?, ?, 'pending')
            """,
            ("I heard drinking cow urine cures all diseases " + SEED_MARKER, "en", _fmt(now - timedelta(hours=6))),
        )
        conn.commit()
        print("Inserted 1 flagged myth row.")

        for phone, lang in [
            ("+91XXXXXXXXX1", "hi"),
            ("+91XXXXXXXXX2", "en"),
            ("+91XXXXXXXXX3", "hi"),
            ("+91XXXXXXXXX4", "kn"),
            ("+91XXXXXXXXX5", "kn"),
            ("+1XXXXXXXXXX6", "en"),
        ]:
            conn.execute(
                """
                INSERT INTO sessions (phone_number, language_code, last_interaction)
                VALUES (?, ?, ?)
                ON CONFLICT(phone_number) DO UPDATE SET
                    language_code    = excluded.language_code,
                    last_interaction = excluded.last_interaction
                """,
                (phone, lang, _fmt(now)),
            )
            row = conn.execute(
                "SELECT last_interaction FROM sessions WHERE phone_number = ?",
                (phone,),
            ).fetchone()
            if row["last_interaction"] != _fmt(now):
                print(f"WARNING: session for {phone} stored {row['last_interaction']!r} instead of {_fmt(now)!r}")

        conn.commit()
        print("Upserted 6 session rows.")

        # Open each demo user's 24-hour reply window. Without this, a
        # dashboard-triggered advisory has no free recipients to reach and
        # reports "no users inside their 24h service window".
        for phone, _ in [
            ("+91XXXXXXXXX1", "hi"),
            ("+91XXXXXXXXX2", "en"),
            ("+91XXXXXXXXX3", "hi"),
            ("+91XXXXXXXXX4", "kn"),
            ("+91XXXXXXXXX5", "kn"),
            ("+1XXXXXXXXXX6", "en"),
        ]:
            database.open_service_window(phone)
        print("Opened 6 service windows (advisory demo).")

    finally:
        conn.close()

    print("\nSeed complete. Run 'streamlit run dashboard.py' to see live charts.")


if __name__ == "__main__":
    run_seed()
