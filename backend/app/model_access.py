import sqlite3

from fastapi import HTTPException, Request


HISTORICAL_MODEL_LABEL = "历史受限模型"


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS project_seedance_access (
            project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id) ON DELETE CASCADE
        )
    """)


def has_seedance_access(connection: sqlite3.Connection, project_id: str) -> bool:
    return connection.execute("SELECT 1 FROM project_seedance_access WHERE project_id = ?", (project_id,)).fetchone() is not None


def set_seedance_access(connection: sqlite3.Connection, project_id: str, enabled: bool) -> None:
    if enabled:
        connection.execute("INSERT OR IGNORE INTO project_seedance_access (project_id) VALUES (?)", (project_id,))
    else:
        connection.execute("DELETE FROM project_seedance_access WHERE project_id = ?", (project_id,))


def require_project_access(connection: sqlite3.Connection, project_id: str) -> None:
    if not has_seedance_access(connection, project_id):
        raise HTTPException(403, {"code": "seedance_access_required", "message": "当前项目未获得此模型能力授权"})


def visible_video_history(request: Request, project_id: str, history: dict) -> dict:
    """Retain historical amounts and totals without advertising disabled model aliases."""
    with request.app.state.db.connect() as connection:
        if has_seedance_access(connection, project_id):
            return history
    for item in history["items"]:
        item["model"] = HISTORICAL_MODEL_LABEL
    if "spendTrend" in history:
        for day in history["spendTrend"]["days"]:
            if day["models"]:
                day["models"] = [{"model": HISTORICAL_MODEL_LABEL, "amount": day["amount"]}]
    return history
