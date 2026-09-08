"""Proactive rumour-spike detection.

detect_rumor_spike() counts queries per category over `window_hours`,
compares the count to the trailing 7-day daily average, and flags
categories that exceed both an absolute `threshold` and a 50% jump over
baseline.

Detected spikes are persisted to the alerts table (deduplicated within the
same hour) and returned so dashboard.py can render an alert banner.

Timestamp contract
------------------
All timestamps in this project are stored as TEXT in the layout
'YYYY-MM-DD HH:MM:SS'.  That layout is ASCII-sortable, so
`timestamp >= '2026-09-08 10:00:00'` works as a range query.  If a row
ever contains an ISO-8601 string with a 'T', range queries may silently
miss or double-count it — alerting.py logs a warning when it spots that.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import database

logger = logging.getLogger(__name__)


def detect_rumor_spike(
    window_hours: int = 24,
    threshold: int = 5,
) -> list[dict]:
    def _fmt(dt: datetime) -> str:
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    baseline_end = window_start
    baseline_start = now - timedelta(days=8)

    try:
        conn = sqlite3.connect(database.DB_PATH)
        conn.row_factory = sqlite3.Row

        spike_rows = conn.execute(
            """
            SELECT category, COUNT(*) as cnt
            FROM logs
            WHERE timestamp >= ?
              AND category IS NOT NULL
            GROUP BY category
            """,
            (_fmt(window_start),),
        ).fetchall()
        if not spike_rows:
            conn.close()
            return []

        sample_ts = conn.execute(
            "SELECT timestamp FROM logs ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if sample_ts and 'T' in sample_ts['timestamp']:
            logger.warning(
                "Logs appear to contain ISO-8601 timestamps; "
                "alerting.py expects 'YYYY-MM-DD HH:MM:SS'."
            )

        baseline_rows = conn.execute(
            """
            SELECT category, COUNT(*) as cnt
            FROM logs
            WHERE timestamp >= ?
              AND timestamp < ?
              AND category IS NOT NULL
            GROUP BY category
            """,
            (_fmt(baseline_start), _fmt(baseline_end)),
        ).fetchall()
        baseline_map: dict[str, float] = {}
        for row in baseline_rows:
            baseline_map[row["category"]] = row["cnt"] / 7.0

        spikes: list[dict] = []
        for row in spike_rows:
            cat = row["category"]
            spike_count = row["cnt"]
            baseline_avg = baseline_map.get(cat, 0.0)

            is_above_threshold = spike_count >= threshold
            is_above_baseline = (
                spike_count > (baseline_avg * 1.5) if baseline_avg > 0
                else spike_count >= threshold
            )

            if is_above_threshold and is_above_baseline:
                one_hour_ago = _fmt(now - timedelta(hours=1))
                existing = conn.execute(
                    """
                    SELECT id FROM alerts
                    WHERE category = ? AND timestamp >= ?
                    """,
                    (cat, one_hour_ago),
                ).fetchone()

                if existing is None:
                    database.log_alert(
                        category=cat,
                        spike_count=spike_count,
                        baseline_avg=round(baseline_avg, 2),
                    )
                    logger.warning(
                        "Rumour spike detected: category='%s' count=%d baseline_avg=%.1f",
                        cat, spike_count, baseline_avg,
                    )

                spikes.append({
                    "category": cat,
                    "spike_count": spike_count,
                    "baseline_avg": round(baseline_avg, 2),
                    "timestamp": _fmt(now),
                })

        conn.close()
        return spikes

    except sqlite3.Error as exc:
        logger.error("detect_rumor_spike database error: %s", exc)
        return []
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("detect_rumor_spike unexpected error: %s", exc)
        return []
