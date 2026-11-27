import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from tempfile import SpooledTemporaryFile
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from . import accounts, key_limits, model_access
from .customer_billing import _csv_cell
from .usage import SHANGHAI, _yuan


router = APIRouter(prefix="/api/customer/usage", tags=["Customer usage"])
DATE_PATTERN = r"^2[0-9]{3}-[0-9]{2}-[0-9]{2}$"
HISTORICAL_MODEL_ID = "historical_video"
ITEM_FIELDS = (
    "id", "keyId", "keyName", "keyPrefix", "keyStatus", "kind", "modelId", "model", "status", "createdAt", "settledAt",
    "promptTokens", "cachedPromptTokens", "outputTokens", "totalTokens", "inputImages", "generatedImages",
    "billingStatus", "pendingReason",
)
CSV_FIELDS = (*ITEM_FIELDS, "outputImageSizes", "listAmount", "amount")
REPORT_ROWS = """
    WITH calls AS (
        SELECT task.id, task.key_id AS keyId, 'video' AS kind,
            CASE WHEN ? THEN task.model ELSE ? END AS modelId,
            CASE WHEN ? THEN task.model ELSE ? END AS model,
            task.status, CAST(strftime('%s', task.created_at) AS INTEGER) AS created_epoch,
            strftime('%Y-%m-%dT%H:%M:%SZ', task.created_at) AS createdAt,
            strftime('%Y-%m-%dT%H:%M:%SZ', task.settled_at) AS settledAt,
            NULL AS promptTokens, NULL AS cachedPromptTokens, task.output_tokens AS outputTokens,
            NULL AS totalTokens, NULL AS inputImages, NULL AS generatedImages, NULL AS imageSizes,
            charge.status AS billingStatus, charge.pending_reason AS pendingReason,
            charge.list_amount_micros AS listMicros, charge.amount_micros AS amountMicros
        FROM video_tasks AS task
        LEFT JOIN video_charges AS charge
            ON charge.task_id = task.id AND charge.project_id = task.project_id AND charge.key_id = task.key_id
        WHERE task.project_id = ? AND datetime(task.created_at) >= datetime(?, 'unixepoch')
            AND datetime(task.created_at) < datetime(?, 'unixepoch')
        UNION ALL
        SELECT call.id, call.key_id, call.modality, call.model, call.model,
            CASE WHEN call.status = 'in_progress' AND call.created_at <= ? THEN 'uncertain' ELSE call.status END,
            call.created_at, strftime('%Y-%m-%dT%H:%M:%SZ', call.created_at, 'unixepoch'),
            strftime('%Y-%m-%dT%H:%M:%SZ', call.settled_at, 'unixepoch'),
            call.prompt_tokens, call.cached_prompt_tokens, call.completion_tokens, call.total_tokens,
            call.input_images, call.generated_images, call.image_sizes_json,
            CASE WHEN call.status = 'succeeded' THEN 'unbilled' ELSE NULL END,
            NULL, NULL, NULL
        FROM general_model_calls AS call
        WHERE call.project_id = ? AND call.created_at >= ? AND call.created_at < ?
    ), report_rows AS (
        SELECT calls.*, key.name AS keyName, key.prefix AS keyPrefix,
            CASE WHEN key.revoked_at IS NULL THEN 'active' ELSE 'revoked' END AS keyStatus
        FROM calls JOIN business_keys AS key ON key.id = calls.keyId AND key.project_id = ?
"""


@dataclass(frozen=True)
class ReportFilters:
    start: date
    end: date
    key_id: str | None
    model: str | None

    def bounds(self) -> tuple[int, int]:
        return (
            int(datetime.combine(self.start, time.min, SHANGHAI).timestamp()),
            int(datetime.combine(self.end + timedelta(days=1), time.min, SHANGHAI).timestamp()),
        )


def _filters(
    key_id: Annotated[str | None, Query(alias="keyId", max_length=132)] = None,
    model: Annotated[str | None, Query(max_length=128)] = None,
    from_date: Annotated[str | None, Query(alias="from", pattern=DATE_PATTERN)] = None,
    to_date: Annotated[str | None, Query(alias="to", pattern=DATE_PATTERN)] = None,
) -> ReportFilters:
    try:
        end = date.fromisoformat(to_date) if to_date else datetime.now(SHANGHAI).date()
        start = date.fromisoformat(from_date) if from_date else end.replace(day=1)
    except ValueError:
        raise HTTPException(422, {"code": "invalid_request", "message": "请输入有效的查询日期"}) from None
    if start > end:
        raise HTTPException(422, {"code": "invalid_date_range", "message": "开始日期不能晚于结束日期"})
    return ReportFilters(start, end, key_id or None, model or None)


ReportQuery = Annotated[ReportFilters, Depends(_filters)]


class _ExactSum:
    """Aggregate integer amounts and counters without SQLite integer overflow."""

    def __init__(self):
        self.total = None

    def step(self, value: int | None) -> None:
        if value is not None:
            self.total = (self.total or 0) + value

    def finalize(self) -> str | None:
        return str(self.total) if self.total is not None else None


def _report_query(
    connection: sqlite3.Connection, project_id: str, options: ReportFilters, video_access: bool,
) -> tuple[str, tuple]:
    if options.key_id and not connection.execute(
        "SELECT 1 FROM business_keys WHERE project_id = ? AND id = ?", (project_id, options.key_id),
    ).fetchone():
        raise HTTPException(404, {"code": "api_key_not_found", "message": "业务 API Key 不存在"})
    start, end = options.bounds()
    conditions = []
    bindings = [
        video_access, HISTORICAL_MODEL_ID, video_access, model_access.HISTORICAL_MODEL_LABEL,
        project_id, start, end, key_limits.general_request_cutoff(int(datetime.now(SHANGHAI).timestamp())),
        project_id, start, end, project_id,
    ]
    for condition, selected in (("key.id = ?", options.key_id), ("calls.modelId = ?", options.model)):
        if selected is not None:
            conditions.append(condition)
            bindings.append(selected)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return REPORT_ROWS + where + ")\n", tuple(bindings)


def _models(connection: sqlite3.Connection, project_id: str, video_access: bool) -> list[dict]:
    rows = connection.execute("""
        SELECT 'video' AS kind, task.model FROM video_tasks AS task
        JOIN business_keys AS key ON key.id = task.key_id AND key.project_id = task.project_id
        WHERE task.project_id = ?
        UNION
        SELECT call.modality AS kind, call.model FROM general_model_calls AS call
        JOIN business_keys AS key ON key.id = call.key_id AND key.project_id = call.project_id
        WHERE call.project_id = ?
        ORDER BY kind, model
    """, (project_id, project_id))
    models = {}
    for row in rows:
        kind, model = row["kind"], row["model"]
        model_id, label = (HISTORICAL_MODEL_ID, model_access.HISTORICAL_MODEL_LABEL) if (
            kind == "video" and not video_access
        ) else (model, model)
        models[kind, model_id] = {"kind": kind, "id": model_id, "label": label}
    return list(models.values())


def _summarize(connection: sqlite3.Connection, query: str, bindings: tuple) -> dict:
    connection.create_aggregate("exact_sum", 1, _ExactSum)
    row = connection.execute(query + """
        SELECT COUNT(*) AS calls, COUNT(CASE WHEN status = 'succeeded' THEN 1 END) AS successful,
            COUNT(CASE WHEN billingStatus = 'priced' THEN 1 END) AS pricedCalls,
            COUNT(CASE WHEN billingStatus = 'pending' THEN 1 END) AS pendingCalls,
            exact_sum(CASE WHEN status = 'succeeded' THEN promptTokens END) AS promptTokens,
            exact_sum(CASE WHEN status = 'succeeded' THEN outputTokens END) AS outputTokens,
            exact_sum(CASE WHEN status = 'succeeded' THEN generatedImages END) AS generatedImages,
            exact_sum(listMicros) AS listMicros, exact_sum(amountMicros) AS amountMicros
        FROM report_rows
    """, bindings).fetchone()
    return {
        **{key: row[key] for key in ("calls", "successful", "pricedCalls", "pendingCalls")},
        **{key: int(row[key]) if row[key] is not None else None for key in ("promptTokens", "outputTokens", "generatedImages")},
        "listAmount": _yuan(int(row["listMicros"])) if row["listMicros"] is not None else None,
        "amount": _yuan(int(row["amountMicros"])) if row["amountMicros"] is not None else None,
    }


def _items(connection: sqlite3.Connection, query: str, bindings: tuple, *, page: int | None = None, page_size: int = 50):
    sql = query + "SELECT * FROM report_rows ORDER BY created_epoch DESC, id DESC"
    if page is not None:
        sql += " LIMIT ? OFFSET ?"
        bindings = (*bindings, page_size, (page - 1) * page_size)
    for row in connection.execute(sql, bindings):
        yield {
            **{name: row[name] for name in ITEM_FIELDS},
            "outputImageSizes": json.loads(row["imageSizes"]) if row["imageSizes"] else None,
            "listAmount": _yuan(row["listMicros"]), "amount": _yuan(row["amountMicros"]),
        }


@router.get("/report")
def report(
    request: Request, customer: accounts.CurrentCustomer, options: ReportQuery,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 50,
) -> dict:
    project_id = customer.project_id
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        video_access = model_access.has_seedance_access(connection, project_id)
        query, bindings = _report_query(connection, project_id, options, video_access)
        summary = _summarize(connection, query, bindings)
        items = list(_items(connection, query, bindings, page=page, page_size=page_size))
        keys = accounts.project_keys(connection, project_id)
        models = _models(connection, project_id, video_access)
    return {
        "from": options.start.isoformat(), "to": options.end.isoformat(), "currency": "CNY",
        "page": page, "pageSize": page_size, "total": summary["calls"], "keys": keys, "models": models,
        "summary": summary, "items": items,
    }


def _csv_chunks(stream):
    with stream:
        while chunk := stream.read(64 * 1024):
            yield chunk


@router.get("/report.csv", response_class=StreamingResponse)
def report_csv(request: Request, customer: accounts.CurrentCustomer, options: ReportQuery) -> StreamingResponse:
    project_id = customer.project_id
    # Stage up to 1 MiB in memory, then spool to a temporary file. Download speed never holds a DB snapshot open.
    stream = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+t", encoding="utf-8", newline="")
    try:
        with request.app.state.db.connect() as connection:
            connection.execute("BEGIN")
            video_access = model_access.has_seedance_access(connection, project_id)
            query, bindings = _report_query(connection, project_id, options, video_access)
            stream.write("\ufeff")
            writer = csv.writer(stream)
            writer.writerow(CSV_FIELDS)
            for item in _items(connection, query, bindings):
                if item["outputImageSizes"] is not None:
                    item["outputImageSizes"] = json.dumps(item["outputImageSizes"], separators=(",", ":"))
                writer.writerow(_csv_cell(item[field]) for field in CSV_FIELDS)
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    filename = f"rich-relay-usage-{options.start.isoformat()}_{options.end.isoformat()}.csv"
    return StreamingResponse(_csv_chunks(stream), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    }, background=BackgroundTask(stream.close))
