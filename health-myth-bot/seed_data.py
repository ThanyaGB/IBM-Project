"""Populate the database with sample log rows.

Usage:
    python seed_data.py

Behaviour:
- Calls database.init_db() to ensure tables exist.
- Clears existing seed rows first (idempotent re-seed).
- Inserts rows spanning English, Hindi, and Swahili across all categories,
  timestamps spread over the last 7 days, with at least 3 emergency rows.
- Also inserts sample feedback, a flagged myth, and session rows.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import database

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
    ("Je, chanjo ya MMR inasababisha ugonjwa wa akili kwa watoto? " + SEED_MARKER, "sw", "Vaccines", False, 160),
    ("Dawa za polio zinafanya watoto kushindwa kuzaa ukubwani? " + SEED_MARKER, "sw", "Vaccines", False, 140),
    ("Sindano ya pepopunda wakati wa ujauzito ni salama? " + SEED_MARKER, "sw", "Maternal Health", False, 110),
    ("Mama mjamzito anapaswa kula mayai au la? " + SEED_MARKER, "sw", "Maternal Health", False, 88),
    ("Mtoto wangu ana homa kali sana, nifanye nini? " + SEED_MARKER, "sw", "Child Health", False, 76),
    ("Antibiotiki zinaponya mafua ya kawaida? " + SEED_MARKER, "sw", "Medication", False, 66),
    ("Je, chanjo ya COVID ina chip ndani yake? " + SEED_MARKER, "sw", "Vaccines", False, 54),
    ("Mtu anapigwa na kifafa na hana fahamu sasa hivi " + SEED_MARKER, "sw", "Emergency", True, 42),
    ("Mtoto wangu amemeza dawa nyingi, hana nguvu " + SEED_MARKER, "sw", "Medication", False, 30),
    ("Mama anayenyonyesha ana virusi vya ukimwi, anaweza kunyonyesha? " + SEED_MARKER, "sw", "Maternal Health", False, 12),
]

assert len(SEED_ROWS) == 30
assert sum(1 for r in SEED_ROWS if r[3]) >= 3


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
            "+254XXXXXXXX4", "+254XXXXXXXX5", "+1XXXXXXXXXX6",
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
            ("+254XXXXXXXX4", "sw"),
            ("+254XXXXXXXX5", "sw"),
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

    finally:
        conn.close()

    print("\nSeed complete. Run 'streamlit run dashboard.py' to see live charts.")


if __name__ == "__main__":
    run_seed()
