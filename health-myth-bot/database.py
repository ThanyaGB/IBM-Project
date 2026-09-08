"""
SQLite persistence layer for health-myth-bot.

Privacy contract:
- Raw phone numbers are stored ONLY in the sessions table (needed to route
  Twilio replies).
- Every other table stores an SHA-256 truncated hash (first 16 hex chars)
  as anonymized_hash.
- Timestamps are TEXT columns in the layout 'YYYY-MM-DD HH:MM:SS'.  SQLite
  is never told to auto-convert TIMESTAMP columns, so values are returned
  exactly as written.
"""

import hashlib
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "health_myth_bot.db")

_write_lock = threading.Lock()


@contextmanager
def _get_conn():
    """Yield a sqlite3 connection and close it on exit.

    detect_types is intentionally NOT set so SQLite never auto-converts
    TIMESTAMP columns.  Timestamps are stored as 'YYYY-MM-DD HH:MM:SS'
    strings and returned as-is.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def _hash_phone(phone_number: str) -> str:
    return hashlib.sha256(phone_number.encode()).hexdigest()[:16]


def init_db() -> None:
    """Create all required tables if they do not already exist.

    Every timestamp column is TEXT (not TIMESTAMP affinity) so the default
    SQLite timestamp converter is never invoked.  This avoids the
    'not enough values to unpack' crash when values contain a 'T' or UTC
    offset.
    """
    with _write_lock, _get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                phone_number      TEXT PRIMARY KEY,
                language_code     TEXT NOT NULL DEFAULT 'en',
                last_interaction  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                anonymized_hash  TEXT NOT NULL,
                query_text       TEXT NOT NULL,
                language         TEXT,
                category         TEXT,
                is_emergency     INTEGER NOT NULL DEFAULT 0,
                timestamp        TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id    INTEGER,
                rating    INTEGER,
                timestamp TEXT    NOT NULL,
                FOREIGN KEY (log_id) REFERENCES logs(id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category     TEXT NOT NULL,
                spike_count  INTEGER NOT NULL,
                baseline_avg REAL    NOT NULL,
                timestamp    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS flagged_myths (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                query_text TEXT NOT NULL,
                language   TEXT,
                timestamp  TEXT    NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        conn.commit()
    logger.info("database: all tables initialised at %s", DB_PATH)


def get_user_language(phone_number: str):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT language_code FROM sessions WHERE phone_number = ?",
            (phone_number,),
        ).fetchone()
    return row["language_code"] if row else None


def set_user_language(phone_number: str, lang_code: str) -> None:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (phone_number, language_code, last_interaction)
            VALUES (?, ?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET
                language_code    = excluded.language_code,
                last_interaction = excluded.last_interaction
            """,
            (phone_number, lang_code, now),
        )
        conn.commit()


def log_query(
    phone_number: str,
    query_text: str,
    language: str,
    category: str,
    is_emergency: bool,
) -> None:
    anon = _hash_phone(phone_number)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO logs
                (anonymized_hash, query_text, language, category, is_emergency, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (anon, query_text, language, category, int(is_emergency), now),
        )
        conn.execute(
            """
            INSERT INTO sessions (phone_number, language_code, last_interaction)
            VALUES (?, 'en', ?)
            ON CONFLICT(phone_number) DO UPDATE SET
                last_interaction = excluded.last_interaction
            """,
            (phone_number, now),
        )
        conn.commit()


def record_feedback(phone_number: str, rating: int) -> None:
    anon = _hash_phone(phone_number)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _write_lock, _get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM logs
            WHERE anonymized_hash = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (anon,),
        ).fetchone()
        if row is None:
            logger.warning(
                "record_feedback: no log row found for hash %s — skipping", anon
            )
            return
        conn.execute(
            "INSERT INTO feedback (log_id, rating, timestamp) VALUES (?, ?, ?)",
            (row["id"], rating, now),
        )
        conn.commit()


def flag_myth(query_text: str, language: str) -> None:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO flagged_myths (query_text, language, timestamp, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (query_text, language, now),
        )
        conn.commit()


def fetch_flagged_myths(status: str = "pending") -> list:
    with _get_conn() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM flagged_myths ORDER BY timestamp DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flagged_myths WHERE status = ? ORDER BY timestamp DESC",
                (status,),
            ).fetchall()
    return [dict(r) for r in rows]


def log_alert(category: str, spike_count: int, baseline_avg: float) -> None:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO alerts (category, spike_count, baseline_avg, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (category, spike_count, baseline_avg, now),
        )
        conn.commit()


def fetch_recent_alerts(limit: int = 20) -> list:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_all_logs(limit: int = 500) -> list:
    """Return up to `limit` most-recent log rows as a list of dicts."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, anonymized_hash, query_text, language,
                   category, is_emergency, timestamp
            FROM logs
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_summary_stats() -> dict:
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        languages = conn.execute(
            "SELECT COUNT(DISTINCT language) FROM logs WHERE language IS NOT NULL"
        ).fetchone()[0]
        emergency = conn.execute(
            "SELECT COUNT(*) FROM logs WHERE is_emergency = 1"
        ).fetchone()[0]

        fb = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) as positive "
            "FROM feedback"
        ).fetchone()
        if fb["total"] and fb["total"] > 0:
            rate = round(fb["positive"] / fb["total"] * 100, 1)
        else:
            rate = None

    return {
        "total_queries": total,
        "active_languages": languages,
        "emergency_count": emergency,
        "helpfulness_rate": rate,
    }
