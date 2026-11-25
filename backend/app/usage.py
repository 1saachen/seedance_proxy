from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from .accounts import CustomerPrincipal, require_customer


router = APIRouter(prefix="/api/customer", tags=["Customer usage"])
SHANGHAI = timezone(timedelta(hours=8))
CHARGES = """
    FROM video_tasks AS task LEFT JOIN video_charges AS charge
        ON charge.task_id = task.id AND charge.project_id = task.project_id AND charge.key_id = task.key_id
"""


def _yuan(micros: int | None) -> str | None:
    return format(Decimal(micros).scaleb(-6), ".6f") if micros is not None else None


def _usage_item(row: sqlite3.Row) -> dict:
    item = {field: row[field] for field in ("id", "model", "status", "outputTokens", "createdAt", "settledAt")}
    if row["billing_status"] is None:
        return {**item, "billing": None}
    return {**item, "billing": {
        "status": row["billing_status"],
        "pendingReason": row["pending_reason"],
        "resolution": row["resolution"],
        "containsVideoInput": bool(row["contains_video_input"]) if row["contains_video_input"] is not None else None,
        "priceVersion": row["price_id"],
        "termsVersion": row["terms_id"],
        "unitPrice": _yuan(row["unit_price_micros_per_million"]),
        "payableBps": row["payable_bps"],
        "listAmount": _yuan(row["list_amount_micros"]),
        "amount": _yuan(row["amount_micros"]),
    }}


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
    where = """WHERE task.project_id = ?
        AND datetime(task.created_at) >= datetime(?) AND datetime(task.created_at) < datetime(?)"""
    scope = (customer.project_id, start.isoformat(), end.isoformat())
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        totals = connection.execute(
            """SELECT COUNT(*) AS calls,
                COUNT(CASE WHEN task.status = 'succeeded' THEN 1 END) AS successful,
                SUM(CASE WHEN task.status = 'succeeded' THEN task.output_tokens END) AS outputTokens,
                COUNT(CASE WHEN charge.status = 'priced' THEN 1 END) AS pricedCalls """ + CHARGES + where,
            scope,
        ).fetchone()
        records = connection.execute(
            """SELECT task.id, task.model, task.status, task.output_tokens AS outputTokens,
                task.created_at AS createdAt, task.settled_at AS settledAt,
                charge.status AS billing_status, charge.pending_reason, charge.resolution,
                charge.contains_video_input, charge.price_id, charge.terms_id,
                charge.unit_price_micros_per_million, charge.payable_bps, charge.list_amount_micros,
                charge.amount_micros """ + CHARGES + where + " ORDER BY task.created_at DESC, task.id DESC LIMIT 100",
            scope,
        ).fetchall()
    return {
        "month": month,
        "currency": "CNY",
        "summary": dict(totals),
        "items": [_usage_item(row) for row in records],
    }
