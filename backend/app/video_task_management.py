import sqlite3
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
import httpx

from . import model_api
from .accounts import BusinessPrincipal, SeedancePrincipal


router = APIRouter(tags=["Video tasks"])
TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "canceled", "expired",
                     "submitting", "submission_unknown", "submission_failed"]
PageNumber = Annotated[int, Query(ge=1, le=500)]
TaskId = Annotated[str, Query(pattern=r"^vid_[A-Za-z0-9_-]{1,128}$")]


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS video_task_deletions (
            task_id TEXT PRIMARY KEY REFERENCES video_tasks(id),
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS video_task_cancellations (
            task_id TEXT PRIMARY KEY REFERENCES video_tasks(id),
            lease_until INTEGER NOT NULL,
            attempted INTEGER NOT NULL DEFAULT 0 CHECK (attempted IN (0, 1)),
            upstream_missing INTEGER NOT NULL DEFAULT 0 CHECK (upstream_missing IN (0, 1))
        );
        CREATE INDEX IF NOT EXISTS video_tasks_key_created ON video_tasks(key_id, created_at, id);
    """)


@router.get(model_api.TASKS_PATH)
def list_tasks(
    request: Request,
    principal: SeedancePrincipal,
    page_num: PageNumber = 1,
    page_size: PageNumber = 20,
    status: Annotated[TaskStatus | None, Query(alias="filter.status")] = None,
    model: Annotated[str | None, Query(alias="filter.model", max_length=80)] = None,
    service_tier: Annotated[Literal["default", "flex"] | None, Query(alias="filter.service_tier")] = None,
    task_ids: Annotated[list[TaskId] | None, Query(alias="filter.task_ids", max_length=500)] = None,
) -> dict:
    if model is not None and model not in model_api.SEEDANCE_MODELS:
        raise model_api._error(422, "invalid_model", "Use a model alias returned by the model list")
    filters = [
        "project_id = ?", "key_id = ?",
        "NOT EXISTS (SELECT 1 FROM video_task_deletions WHERE task_id = video_tasks.id)",
    ]
    parameters = [principal.project_id, principal.key_id]
    for clause, value in (
        ("status = ?", "canceled" if status == "cancelled" else status),
        ("model = ?", model),
        ("COALESCE(json_extract(result_json, '$.service_tier'), 'default') = ?", service_tier),
    ):
        if value is not None:
            filters.append(clause)
            parameters.append(value)
    if task_ids:
        filters.append(f"id IN ({', '.join(['?'] * len(task_ids))})")
        parameters.extend(task_ids)
    matching = " FROM video_tasks WHERE " + " AND ".join(filters)
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*)" + matching, parameters).fetchone()[0]
        rows = connection.execute(
            "SELECT video_tasks.*, " + model_api.CANCELLATION_PENDING_FIELD + matching +
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*parameters, page_size, (page_num - 1) * page_size),
        ).fetchall()
    return {"total": total, "items": [model_api._public_task(row) for row in rows]}


def _deletable_task(request: Request, principal: BusinessPrincipal, task_id: str) -> sqlite3.Row:
    with request.app.state.db.connect() as connection:
        row = connection.execute("""
            SELECT video_tasks.*, video_task_deletions.deleted_at AS deleted_at,
                   COALESCE(video_task_cancellations.attempted, 0) AS cancellation_attempted
            FROM video_tasks LEFT JOIN video_task_deletions ON video_task_deletions.task_id = video_tasks.id
            LEFT JOIN video_task_cancellations ON video_task_cancellations.task_id = video_tasks.id
            WHERE video_tasks.id = ? AND project_id = ? AND key_id = ?
        """, (task_id, principal.project_id, principal.key_id)).fetchone()
    if row is None:
        raise model_api._error(404, "video_task_not_found", "Video task was not found")
    return row


@router.delete(model_api.TASKS_PATH + "/{task_id}")
async def delete_task(
    request: Request,
    principal: SeedancePrincipal,
    task_id: str,
    delete_cancelled: bool = False,
) -> dict:
    row = _deletable_task(request, principal, task_id)
    if row["deleted_at"] is not None or (row["status"] == "canceled" and not delete_cancelled):
        return {}
    if row["status"] in model_api.TERMINAL_STATES or row["status"] == "submission_failed":
        # Keep original task and charge data for usage, statements and idempotency.
        with request.app.state.db.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO video_task_deletions(task_id) VALUES (?)", (task_id,))
        return {}
    if row["status"] != "queued":
        raise model_api._error(409, "video_task_not_cancellable", "Only queued tasks can be cancelled")

    now = int(time.time())
    lease = now + 90
    with request.app.state.db.connect() as connection:
        claimed = connection.execute("""
            INSERT INTO video_task_cancellations (task_id, lease_until) VALUES (?, ?)
            ON CONFLICT (task_id) DO UPDATE SET lease_until = excluded.lease_until
            WHERE video_task_cancellations.lease_until <= ? RETURNING task_id
        """, (task_id, lease, now)).fetchone()
    if claimed is None:
        raise model_api._error(409, "video_cancellation_pending", "A cancellation is already in progress")
    try:
        return await _cancel_queued_task(request, principal, task_id)
    finally:
        with request.app.state.db.connect() as connection:
            connection.execute("""
                DELETE FROM video_task_cancellations WHERE task_id = ? AND lease_until = ? AND attempted = 0
            """, (task_id, lease))
            connection.execute("UPDATE video_task_cancellations SET lease_until = 0 WHERE task_id = ? AND lease_until = ?",
                               (task_id, lease))


async def _cancel_queued_task(request: Request, principal: BusinessPrincipal, task_id: str) -> dict:
    row = _deletable_task(request, principal, task_id)
    if row["status"] == "canceled":
        return {}
    if row["status"] != "queued":
        raise model_api._error(409, "video_task_not_cancellable", "Task is no longer queued")

    headers = model_api._ark_headers(request.app)
    # Commit any intervening terminal result before attempting Ark's state-dependent DELETE.
    await model_api._refresh_task(request.app, row, headers)
    row = _deletable_task(request, principal, task_id)
    if row["status"] == "canceled":
        return {}
    if row["status"] != "queued":
        raise model_api._error(409, "video_task_not_cancellable", "Task is no longer queued")
    if row["cancellation_attempted"]:
        raise model_api._error(502, "video_cancellation_unknown", "Cancellation could not be confirmed")
    # Persist before sending, so a process exit cannot cause a second state-dependent DELETE.
    with request.app.state.db.connect() as connection:
        connection.execute("UPDATE video_task_cancellations SET attempted = 1 WHERE task_id = ?", (task_id,))
    try:
        response = await request.app.state.ark.delete(f"{model_api.TASKS_PATH}/{row['upstream_id']}", headers=headers)
    except httpx.RequestError:
        # The next refresh may confirm a cancellation after a lost DELETE response.
        response = None
    if response is not None and 400 <= response.status_code < 500 and response.status_code not in (404, 408):
        with request.app.state.db.connect() as connection:
            connection.execute("UPDATE video_task_cancellations SET attempted = 0, upstream_missing = 0 WHERE task_id = ?",
                               (task_id,))
    try:
        await model_api._refresh_task(request.app, row, headers)
    except HTTPException:
        raise model_api._error(502, "video_cancellation_unknown", "Cancellation could not be confirmed") from None

    current = _deletable_task(request, principal, task_id)
    if current["status"] != "canceled":
        if current["status"] != "queued" or (response is not None and response.status_code in (400, 409)):
            raise model_api._error(409, "video_task_not_cancellable", "Task is no longer cancellable")
        raise model_api._error(502, "video_cancellation_unknown", "Cancellation could not be confirmed")
    return {}
