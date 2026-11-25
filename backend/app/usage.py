from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from .accounts import CustomerPrincipal, require_customer


router = APIRouter(prefix="/api/customer", tags=["Customer usage"])
SHANGHAI = timezone(timedelta(hours=8))


@router.get("/usage")
def customer_usage(
    request: Request,
    customer: Annotated[CustomerPrincipal, Depends(require_customer)],
    month: Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])$")] = None,
):
    month = month or datetime.now(SHANGHAI).strftime("%Y-%m")
    year, calendar_month = map(int, month.split("-"))
    start = datetime(year, calendar_month, 1, tzinfo=SHANGHAI)
    end = datetime(year + (calendar_month == 12), calendar_month % 12 + 1, 1, tzinfo=SHANGHAI)
    where = "WHERE project_id = ? AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
    scope = (customer.project_id, start.isoformat(), end.isoformat())
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        totals = connection.execute(
            "SELECT COUNT(*) AS calls, COUNT(CASE WHEN status = 'succeeded' THEN 1 END) AS successful, "
            "SUM(CASE WHEN status = 'succeeded' THEN output_tokens END) AS outputTokens "
            "FROM video_tasks " + where,
            scope,
        ).fetchone()
        records = connection.execute(
            "SELECT id, model, status, output_tokens AS outputTokens, "
            "created_at AS createdAt, settled_at AS settledAt FROM video_tasks "
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT 100",
            scope,
        ).fetchall()
    return {
        "month": month,
        "currency": "CNY",
        "summary": {**dict(totals), "pricedCalls": 0},
        "items": [dict(row) for row in records],
    }
