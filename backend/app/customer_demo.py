from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from .config import Settings


DEMO_PROJECT_ID = "prj_mock_workbench"


def _demo_database_path(settings: Settings) -> Path | None:
    path = settings.customer_demo_database_path
    if path is None:
        return None
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or not os.access(path, os.R_OK) or path.samefile(settings.database_path):
            return None
    except (OSError, RuntimeError):
        return None
    return path


@contextmanager
def _demo_connection(path: Path) -> Iterator[sqlite3.Connection]:
    # The deployment publishes a complete, standalone snapshot. Open it without creating sidecar files.
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        yield connection


def _snapshot_date(connection: sqlite3.Connection) -> date:
    newest = connection.execute("""
        SELECT MAX(date(created_at, '+08:00')) FROM video_tasks WHERE project_id = ?
    """, (DEMO_PROJECT_ID,)).fetchone()[0]
    try:
        return date.fromisoformat(newest or "")
    except ValueError:
        raise sqlite3.DataError("Invalid demo snapshot date") from None


def usage_unavailable() -> HTTPException:
    return HTTPException(503, {"code": "usage_unavailable", "message": "暂时无法读取用量，请稍后重试"})


@contextmanager
def customer_read_scope(request: Request, project_id: str) -> Iterator[tuple[sqlite3.Connection, str, date | None]]:
    settings = request.app.state.settings
    if project_id != settings.customer_demo_project_id:
        with request.app.state.db.connect() as connection:
            yield connection, project_id, None
        return

    path = _demo_database_path(settings)
    if path is None:
        raise usage_unavailable()
    try:
        with _demo_connection(path) as connection:
            yield connection, DEMO_PROJECT_ID, _snapshot_date(connection)
    except sqlite3.Error:
        raise usage_unavailable() from None
