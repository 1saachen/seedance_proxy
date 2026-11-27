from datetime import date, datetime, time, timedelta, timezone
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .accounts import CustomerPrincipal, require_customer


router = APIRouter(prefix="/api/customer", tags=["Customer usage"])
SHANGHAI = timezone(timedelta(hours=8))
CHARGES = """
    FROM video_tasks AS task LEFT JOIN video_charges AS charge
        ON charge.task_id = task.id AND charge.project_id = task.project_id AND charge.key_id = task.key_id
"""
CREATED_SCOPE = """
    WHERE task.project_id = ?
        AND datetime(task.created_at) >= datetime(?) AND datetime(task.created_at) < datetime(?)
"""


def _yuan(micros: int | None) -> str | None:
    if micros is None:
        return None
    yuan, fraction = divmod(abs(micros), 1_000_000)
    sign = "-" if micros < 0 else ""
    return f"{sign}{yuan}.{fraction:06d}"


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


def _created_scope(project_id: str, start: date, end: date) -> tuple[str, str, str]:
    return (
        project_id,
        datetime.combine(start, time.min, SHANGHAI).isoformat(),
        datetime.combine(end, time.min, SHANGHAI).isoformat(),
    )


def _spend_trend(connection: sqlite3.Connection, project_id: str, today: date) -> dict:
    first_day = today - timedelta(days=6)
    days = {
        (first_day + timedelta(days=offset)).isoformat(): {"pricedCalls": 0, "pendingCalls": 0, "models": {}}
        for offset in range(7)
    }
    charges = connection.execute(
        """SELECT date(task.created_at, '+08:00') AS usage_day, charge.model, charge.status, charge.amount_micros """
        + CHARGES + CREATED_SCOPE + " AND charge.status IN ('priced', 'pending')",
        _created_scope(project_id, first_day, today + timedelta(days=1)),
    )
    for charge in charges:
        day = days[charge["usage_day"]]
        if charge["status"] == "pending":
            day["pendingCalls"] += 1
            continue
        day["pricedCalls"] += 1
        model = charge["model"]
        costs = day["models"]
        costs[model] = costs.get(model, 0) + charge["amount_micros"]

    # SQLite's integer SUM can overflow when individually valid charges are added together.
    # Accumulate microunits as Python integers, and format only for the response.
    total_micros = None
    trend = []
    for day, counts in days.items():
        costs = counts["models"]
        amount_micros = sum(costs.values()) if costs else None
        if amount_micros is not None:
            total_micros = (total_micros or 0) + amount_micros
        trend.append({
            "date": day,
            "amount": _yuan(amount_micros),
            "pricedCalls": counts["pricedCalls"],
            "pendingCalls": counts["pendingCalls"],
            "models": [{"model": model, "amount": _yuan(costs[model])} for model in sorted(costs)],
        })
    return {"totalAmount": _yuan(total_micros), "days": trend}


@router.get("/usage")
def customer_usage(
    request: Request,
    customer: Annotated[CustomerPrincipal, Depends(require_customer)],
    month: Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])$")] = None,
    day: Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")] = None,
    include_trend: Annotated[bool, Query(alias="includeTrend")] = False,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 100,
):
    today = datetime.now(SHANGHAI).date()
    if day is not None:
        try:
            start = date.fromisoformat(day)
        except ValueError as error:
            raise HTTPException(422, {"code": "invalid_usage_day", "message": "请选择有效的使用日期"}) from error
        day_month = start.strftime("%Y-%m")
        if month is not None and month != day_month:
            raise HTTPException(422, {"code": "usage_day_month_mismatch", "message": "日期须属于所选月份"})
        month = day_month
        end = start + timedelta(days=1)
    else:
        month = month or today.strftime("%Y-%m")
        year, calendar_month = map(int, month.split("-"))
        start = date(year, calendar_month, 1)
        end = date(year + (calendar_month == 12), calendar_month % 12 + 1, 1)
    scope = _created_scope(customer.project_id, start, end)
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        totals = connection.execute(
            """SELECT COUNT(*) AS calls,
                COUNT(CASE WHEN task.status = 'succeeded' THEN 1 END) AS successful,
                SUM(CASE WHEN task.status = 'succeeded' THEN task.output_tokens END) AS outputTokens,
                COUNT(CASE WHEN charge.status = 'priced' THEN 1 END) AS pricedCalls """ + CHARGES + CREATED_SCOPE,
            scope,
        ).fetchone()
        records = connection.execute(
            """SELECT task.id, task.model, task.status, task.output_tokens AS outputTokens,
                task.created_at AS createdAt, task.settled_at AS settledAt,
                charge.status AS billing_status, charge.pending_reason, charge.resolution,
                charge.contains_video_input, charge.price_id, charge.terms_id,
                charge.unit_price_micros_per_million, charge.payable_bps, charge.list_amount_micros,
                charge.amount_micros """ + CHARGES + CREATED_SCOPE
                + " ORDER BY datetime(task.created_at) DESC, task.id DESC LIMIT ? OFFSET ?",
            (*scope, page_size, (page - 1) * page_size),
        ).fetchall()
        trend = {"spendTrend": _spend_trend(connection, customer.project_id, today)} if include_trend else {}
    return {
        "month": month,
        "currency": "CNY",
        "page": page,
        "pageSize": page_size,
        "summary": dict(totals),
        "items": [_usage_item(row) for row in records],
        **({"day": day} if day is not None else {}),
        **trend,
    }
