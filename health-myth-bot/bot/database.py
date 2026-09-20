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
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# The database lives under data/ so the project root stays code-only; both
# are override-friendly via env (tests point DB_PATH at a temp dir).
DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "health_myth_bot.db"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# How long a half-finished conversation ("reply YES to report", "rate this
# answer") stays valid.  Without an expiry, a user who never answers keeps a
# stale pending state that swallows their next real question.
PENDING_STATE_TTL_MINUTES = 30

_write_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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

            -- Messaging providers retry webhooks.  Recording the provider's
            -- message id keeps a retry from re-running the LLM and
            -- double-logging the same question.
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                timestamp  TEXT NOT NULL
            );

            -- Pending conversational state lives in the database, not in
            -- module globals, so a restart does not lose (or wrongly keep) it.
            CREATE TABLE IF NOT EXISTS pending_state (
                phone_number TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                payload      TEXT,
                created_at   TEXT NOT NULL
            );

            -- When a user last messaged us.  WhatsApp charges per *template*
            -- message sent outside the 24-hour window a user's own message
            -- opens; every non-template reply inside it is free.  Tracking the
            -- window is what lets proactive advisories stay free instead of
            -- quietly becoming a paid feature.
            CREATE TABLE IF NOT EXISTS service_windows (
                phone_number TEXT PRIMARY KEY,
                opened_at    TEXT NOT NULL
            );

            -- Runtime toggles the dashboard can flip without a redeploy
            -- (bot_enabled, active_advisory_category, ...).
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- One row per proactive advisory run, so a spike that keeps
            -- crossing the threshold does not re-message the same people.
            -- sent_count/skipped_count make the free-window trade-off visible.
            CREATE TABLE IF NOT EXISTS advisories (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category      TEXT NOT NULL,
                body          TEXT NOT NULL,
                sent_count    INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                timestamp     TEXT    NOT NULL
            );
            """
        )
        conn.commit()
        _migrate(conn)
    logger.info("database: all tables initialised at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS cannot add to old files.

    Existing installs already have a flagged_myths table without review_note,
    and ALTER TABLE is the only portable way to add it without losing rows.
    """
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(flagged_myths)").fetchall()
    }
    if "review_note" not in columns:
        conn.execute("ALTER TABLE flagged_myths ADD COLUMN review_note TEXT")
        conn.commit()
        logger.info("database: added flagged_myths.review_note")


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


def is_message_processed(message_id: str) -> bool:
    """True if this provider message id has already been handled."""
    if not message_id:
        return False
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return row is not None


def mark_message_processed(message_id: str) -> None:
    """Record a provider message id as handled (idempotent)."""
    if not message_id:
        return
    with _write_lock, _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (message_id, timestamp) "
            "VALUES (?, ?)",
            (message_id, _utcnow()),
        )
        conn.commit()


def set_pending_state(
    phone_number: str,
    kind: str,
    payload: str | None = None,
    ttl_minutes: int = PENDING_STATE_TTL_MINUTES,
) -> None:
    """Remember that we are waiting for a reply of type `kind` from a user."""
    now = _utcnow()
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            DELETE FROM pending_state
            WHERE created_at < ?
            """,
            (_cutoff(ttl_minutes),),
        )
        conn.execute(
            """
            INSERT INTO pending_state (phone_number, kind, payload, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET
                kind       = excluded.kind,
                payload    = excluded.payload,
                created_at = excluded.created_at
            """,
            (phone_number, kind, payload, now),
        )
        conn.commit()


def get_pending_state(
    phone_number: str,
    kind: str,
    ttl_minutes: int = PENDING_STATE_TTL_MINUTES,
) -> str | None:
    """Return the stored payload if we are still waiting for `kind`.

    Returns None when nothing is pending, when the pending state belongs to
    a different conversation step, or when it has expired.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT kind, payload, created_at FROM pending_state "
            "WHERE phone_number = ?",
            (phone_number,),
        ).fetchone()

    if row is None or row["kind"] != kind:
        return None
    if row["created_at"] < _cutoff(ttl_minutes):
        clear_pending_state(phone_number, kind)
        return None
    return row["payload"]


def clear_pending_state(phone_number: str, kind: str | None = None) -> None:
    """Drop the pending state for a user (optionally only for one `kind`)."""
    with _write_lock, _get_conn() as conn:
        if kind is None:
            conn.execute(
                "DELETE FROM pending_state WHERE phone_number = ?",
                (phone_number,),
            )
        else:
            conn.execute(
                "DELETE FROM pending_state WHERE phone_number = ? AND kind = ?",
                (phone_number, kind),
            )
        conn.commit()


def _cutoff(ttl_minutes: int) -> str:
    """Timestamp `ttl_minutes` ago, in the same sortable text format."""
    return (
        datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _hours_ago(hours: float) -> str:
    """Timestamp `hours` ago, in the same sortable text format."""
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Service window (the free-reply window, and the guard against paid sends)
# ---------------------------------------------------------------------------
def open_service_window(phone_number: str) -> None:
    """Record that the user just messaged us, opening their 24h window."""
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO service_windows (phone_number, opened_at)
            VALUES (?, ?)
            ON CONFLICT(phone_number) DO UPDATE SET opened_at = excluded.opened_at
            """,
            (phone_number, _utcnow()),
        )
        conn.commit()


def service_window_open(phone_number: str, window_hours: float = 24.0) -> bool:
    """True when the user is still inside their free customer-service window."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT opened_at FROM service_windows WHERE phone_number = ?",
            (phone_number,),
        ).fetchone()
    if row is None:
        return False
    return row["opened_at"] >= _hours_ago(window_hours)


def fetch_users_in_window(window_hours: float = 24.0) -> list[str]:
    """Phone numbers currently inside their free reply window."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT phone_number FROM service_windows WHERE opened_at >= ?",
            (_hours_ago(window_hours),),
        ).fetchall()
    return [r["phone_number"] for r in rows]


def within_hours(timestamp: str | None, hours: float) -> bool:
    """True when a stored timestamp is newer than `hours` ago.

    Public because callers (advisory cooldowns) need the comparison but not
    the private formatter, and reaching into ``_hours_ago`` from another module
    is how the format silently drifts apart.
    """
    if not timestamp:
        return False
    return timestamp >= _hours_ago(hours)


def count_recent_queries(phone_number: str, hours: float = 1.0) -> int:
    """How many questions this user has asked in the last `hours`.

    Backs the per-user rate limit: a person asking eleven questions an hour is
    either in distress or is a script, and both deserve a pause rather than
    another LLM call.
    """
    anon = _hash_phone(phone_number)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM logs "
            "WHERE anonymized_hash = ? AND timestamp >= ?",
            (anon, _hours_ago(hours)),
        ).fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Runtime settings (kill switch and friends)
# ---------------------------------------------------------------------------
def set_setting(key: str, value: str) -> None:
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        conn.commit()


def get_setting(key: str, default: str | None = None) -> str | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def bot_enabled() -> bool:
    """The kill switch: env default, overridable at runtime from the dashboard.

    Checked before anything expensive, so pausing the bot stops LLM spend and
    stops replying — not just hides a banner.
    """
    override = get_setting("bot_enabled")
    if override is None:
        env_default = os.environ.get("BOT_ENABLED", "true")
        return env_default.strip().lower() in {"1", "true", "yes", "on"}
    return override.strip().lower() in {"1", "true", "yes", "on"}


def set_bot_enabled(enabled: bool) -> None:
    set_setting("bot_enabled", "true" if enabled else "false")


# ---------------------------------------------------------------------------
# Flagged-myth review loop
# ---------------------------------------------------------------------------
def fetch_flagged_myth(flag_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM flagged_myths WHERE id = ?", (flag_id,)
        ).fetchone()
    return dict(row) if row else None


def resolve_flagged_myth(
    flag_id: int, status: str, review_note: str | None = None
) -> None:
    """Mark a flagged myth reviewed / rejected (dashboard review loop)."""
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError(f"invalid review status: {status!r}")
    with _write_lock, _get_conn() as conn:
        conn.execute(
            "UPDATE flagged_myths SET status = ?, review_note = ? WHERE id = ?",
            (status, review_note, flag_id),
        )
        conn.commit()


def count_flagged_myths(status: str = "pending") -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM flagged_myths WHERE status = ?", (status,)
        ).fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Proactive advisories
# ---------------------------------------------------------------------------
def log_advisory(
    category: str, body: str, sent_count: int, skipped_count: int
) -> None:
    with _write_lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO advisories
                (category, body, sent_count, skipped_count, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (category, body, sent_count, skipped_count, _utcnow()),
        )
        conn.commit()


def fetch_recent_advisories(limit: int = 20) -> list:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM advisories ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def last_advisory_at(category: str) -> str | None:
    """Timestamp of the most recent advisory for `category`, if any."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT timestamp FROM advisories WHERE category = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (category,),
        ).fetchone()
    return row["timestamp"] if row else None


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
        "pending_reviews": count_flagged_myths("pending"),
        "users_in_window": len(fetch_users_in_window()),
    }
