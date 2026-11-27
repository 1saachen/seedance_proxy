import sqlite3
import time

from fastapi import HTTPException


MAX_CONCURRENCY_LIMIT = 1000
MAX_RPM_LIMIT = 60_000
MICROSECONDS_PER_SECOND = 1_000_000
RPM_WINDOW_US = 60 * MICROSECONDS_PER_SECOND
UNCONFIRMED_TASK_LIFETIME_SECONDS = 72 * 3600
GENERAL_REQUEST_LIFETIME_SECONDS = 180
GENERAL_REQUEST_CLEANUP_GRACE_SECONDS = 5

_ACTIVE_STATUSES = "status IN ('submitting', 'submission_unknown', 'queued', 'running')"
_ACTIVE_TASK = (
    _ACTIVE_STATUSES + " AND (status IN ('queued', 'running') OR datetime(created_at) > datetime(?, 'unixepoch'))"
)


# Both reservation and metrics use the same predicates and cutoff instant.
_ACTIVE_COUNTS = f"""
    (SELECT COUNT(*) FROM video_tasks
     WHERE key_id = business_keys.id AND {_ACTIVE_TASK}) AS activeTasks,
    (SELECT COUNT(*) FROM general_model_calls
     WHERE key_id = business_keys.id AND status = 'in_progress' AND modality = 'text'
         AND created_at > ?) AS activeTextRequests,
    (SELECT COUNT(*) FROM general_model_calls
     WHERE key_id = business_keys.id AND status = 'in_progress' AND modality = 'image'
         AND created_at > ?) AS activeImageRequests
"""
_COUNT_FIELDS = ("activeTasks", "activeTextRequests", "activeImageRequests")


def _now_us() -> int:
    return time.time_ns() // 1_000


def _submission_cutoff(now_us: int) -> int:
    return now_us // MICROSECONDS_PER_SECOND - UNCONFIRMED_TASK_LIFETIME_SECONDS


def general_request_cutoff(now: int) -> int:
    return now - GENERAL_REQUEST_LIFETIME_SECONDS - GENERAL_REQUEST_CLEANUP_GRACE_SECONDS


def _active_cutoffs(now_us: int) -> tuple[int, int, int]:
    general_cutoff = general_request_cutoff(now_us // MICROSECONDS_PER_SECOND)
    return _submission_cutoff(now_us), general_cutoff, general_cutoff


def _total_active(row: sqlite3.Row) -> int:
    return sum(row[field] for field in _COUNT_FIELDS)


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS business_key_limits (
            key_id TEXT PRIMARY KEY REFERENCES business_keys(id) ON DELETE CASCADE,
            concurrency_limit INTEGER CHECK (concurrency_limit BETWEEN 1 AND {MAX_CONCURRENCY_LIMIT}),
            rpm_limit INTEGER CHECK (rpm_limit BETWEEN 1 AND {MAX_RPM_LIMIT})
        );
        CREATE TABLE IF NOT EXISTS business_key_requests (
            id INTEGER PRIMARY KEY,
            key_id TEXT NOT NULL REFERENCES business_keys(id) ON DELETE CASCADE,
            accepted_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS business_key_requests_key_time ON business_key_requests(key_id, accepted_at);
        CREATE INDEX IF NOT EXISTS business_key_requests_time ON business_key_requests(accepted_at);
        CREATE INDEX IF NOT EXISTS video_tasks_key_active ON video_tasks(key_id, status, created_at)
            WHERE {_ACTIVE_STATUSES};
    """)
    connection.execute("DELETE FROM business_key_requests WHERE accepted_at <= ?", (_now_us() - RPM_WINDOW_US,))


def set_limits(
    connection: sqlite3.Connection, key_id: str, concurrency_limit: int | None, rpm_limit: int | None,
) -> None:
    for value, maximum in ((concurrency_limit, MAX_CONCURRENCY_LIMIT), (rpm_limit, MAX_RPM_LIMIT)):
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise ValueError("Invalid business Key limit")
    if concurrency_limit is None and rpm_limit is None:
        connection.execute("DELETE FROM business_key_limits WHERE key_id = ?", (key_id,))
        return
    connection.execute("""
        INSERT INTO business_key_limits (key_id, concurrency_limit, rpm_limit)
        VALUES (?, ?, ?)
        ON CONFLICT (key_id) DO UPDATE
        SET concurrency_limit = excluded.concurrency_limit, rpm_limit = excluded.rpm_limit
    """, (key_id, concurrency_limit, rpm_limit))


def admit_request(connection: sqlite3.Connection, key_id: str) -> int | None:
    """Count one admission or return Retry-After seconds. The caller holds the write transaction."""
    now = _now_us()
    cutoff = now - RPM_WINDOW_US
    connection.execute("DELETE FROM business_key_requests WHERE accepted_at <= ?", (cutoff,))
    limits = connection.execute("SELECT rpm_limit FROM business_key_limits WHERE key_id = ?", (key_id,)).fetchone()
    if limits is not None and limits["rpm_limit"] is not None:
        # Only the most recent N events matter, including after an operator lowers the limit.
        last_slot = connection.execute("""
            SELECT accepted_at FROM business_key_requests
            WHERE key_id = ? AND accepted_at > ? ORDER BY accepted_at DESC LIMIT 1 OFFSET ?
        """, (key_id, cutoff, limits["rpm_limit"] - 1)).fetchone()
        if last_slot is not None:
            remaining = last_slot["accepted_at"] + RPM_WINDOW_US - now
            return (remaining + MICROSECONDS_PER_SECOND - 1) // MICROSECONDS_PER_SECOND
    connection.execute(
        "INSERT INTO business_key_requests (key_id, accepted_at) VALUES (?, ?)", (key_id, now),
    )
    return None


def enforce_concurrency(connection: sqlite3.Connection, key_id: str) -> None:
    """Check all model capacity in the same write transaction as the new reservation."""
    limits = connection.execute(
        "SELECT concurrency_limit FROM business_key_limits WHERE key_id = ?", (key_id,),
    ).fetchone()
    if limits is None or limits["concurrency_limit"] is None:
        return
    counts = connection.execute(
        f"SELECT {_ACTIVE_COUNTS} FROM business_keys WHERE id = ?", (*_active_cutoffs(_now_us()), key_id),
    ).fetchone()
    if _total_active(counts) >= limits["concurrency_limit"]:
        raise HTTPException(429, {"code": "key_concurrency_exceeded", "message": "API Key reached its concurrency limit"})


def project_key_metrics(
    connection: sqlite3.Connection, project_id: str, *, key_id: str | None = None,
) -> dict[str, dict]:
    now = _now_us()
    parameters = (*_active_cutoffs(now), now - RPM_WINDOW_US, project_id)
    key_filter = ""
    if key_id is not None:
        key_filter = " AND business_keys.id = ?"
        parameters = (*parameters, key_id)
    rows = connection.execute(f"""
        SELECT business_keys.id, limits.concurrency_limit AS concurrencyLimit, limits.rpm_limit AS rpmLimit,
            {_ACTIVE_COUNTS},
            (SELECT COUNT(*) FROM business_key_requests
             WHERE key_id = business_keys.id AND accepted_at > ?) AS requestsLastMinute
        FROM business_keys LEFT JOIN business_key_limits AS limits ON limits.key_id = business_keys.id
        WHERE business_keys.project_id = ?{key_filter}
    """, parameters)
    return {
        row["id"]: {
            **{name: row[name] for name in row.keys() if name != "id"}, "activeConcurrentRequests": _total_active(row),
        }
        for row in rows
    }
