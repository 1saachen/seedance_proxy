from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .accounts import CurrentCustomer


router = APIRouter(prefix="/api/customer", tags=["Customer tasks"])
TaskStatus = Literal[
    "submitting", "submission_unknown", "submission_failed", "queued", "running",
    "succeeded", "failed", "canceled", "expired",
]
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
TASK_ERRORS = {
    "submission_unknown": {"code": "video_submission_unknown", "message": "视频任务提交结果待确认"},
    "submission_failed": {"code": "video_submission_failed", "message": "视频任务提交失败"},
    "failed": {"code": "video_generation_failed", "message": "视频生成失败"},
    "canceled": {"code": "video_task_canceled", "message": "视频任务已取消"},
    "expired": {"code": "video_task_expired", "message": "视频任务已过期"},
}


@router.get("/tasks")
def customer_tasks(
    request: Request,
    customer: CurrentCustomer,
    task_id: Annotated[str | None, Query(alias="taskId", pattern=r"^vid_[A-Za-z0-9_-]{1,128}$")] = None,
    from_date: Annotated[str | None, Query(alias="fromDate", pattern=DATE_PATTERN)] = None,
    to_date: Annotated[str | None, Query(alias="toDate", pattern=DATE_PATTERN)] = None,
    status: TaskStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
):
    try:
        start = date.fromisoformat(from_date) if from_date is not None else None
        end = date.fromisoformat(to_date) if to_date is not None else None
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_request", "message": "请求参数无效"}) from None
    if start is not None and end is not None and start > end:
        raise HTTPException(422, detail={"code": "invalid_date_range", "message": "开始日期不能晚于结束日期"})

    filters = ["project_id = ?", "NOT EXISTS (SELECT 1 FROM video_task_deletions WHERE task_id = video_tasks.id)"]
    parameters = [customer.project_id]
    # SQLite treats the legacy timezone-free timestamps as UTC and honors offsets in ISO timestamps.
    for condition, value in (
        ("id = ?", task_id),
        ("date(created_at, '+8 hours') >= ?", from_date),
        ("date(created_at, '+8 hours') <= ?", to_date),
        ("status = ?", status),
    ):
        if value is not None:
            filters.append(condition)
            parameters.append(value)
    matching_tasks = "FROM video_tasks WHERE " + " AND ".join(filters)
    offset = (page - 1) * page_size
    with request.app.state.db.connect() as connection:
        # The count and page use the same snapshot while the poller updates tasks.
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) " + matching_tasks, parameters).fetchone()[0]
        records = []
        if offset < total:
            records = connection.execute(
                """SELECT id, model, status, output_tokens AS outputTokens,
                    created_at AS createdAt, settled_at AS settledAt """ + matching_tasks +
                " ORDER BY julianday(created_at) DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()
    return {
        "items": [{**dict(row), "error": TASK_ERRORS.get(row["status"])} for row in records],
        "page": page,
        "pageSize": page_size,
        "total": total,
    }
