import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from uuid import uuid4

from .billing import SQLITE_MAX_INTEGER


BEIJING = timezone(timedelta(hours=8))
SQLITE_DATETIME = "%Y-%m-%d %H:%M:%S"
MONTH = re.compile(r"2[0-9]{3}-(0[1-9]|1[0-2])")
MICROS_PER_FEN = 10_000

LIVE_SCOPE = """
    FROM video_charges AS charge
    WHERE charge.project_id = ? AND charge.status IN ('priced', 'pending')
        AND datetime(charge.settled_at) >= ? AND datetime(charge.settled_at) < ?
        AND NOT EXISTS (SELECT 1 FROM billing_statement_lines AS line WHERE line.task_id = charge.task_id)
"""
LIVE_LINES = """
    SELECT charge.task_id, charge.key_id, charge.model,
        strftime('%Y-%m', charge.settled_at, '+8 hours') AS source_month,
        CASE WHEN datetime(charge.settled_at) < ? THEN 'late_usage' ELSE 'usage' END AS kind,
        datetime(charge.settled_at) AS settled_at, charge.output_tokens, charge.price_id, charge.terms_id,
        charge.unit_price_micros_per_million, charge.payable_bps, charge.list_amount_micros, charge.amount_micros
"""


class StatementError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _Totals:
    usage_calls: int
    late_calls: int
    pending_calls: int
    list_amount_micros: int
    amount_micros: int
    payable_fen: int


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS billing_statements (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            month TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            usage_calls INTEGER NOT NULL CHECK (usage_calls >= 0),
            late_calls INTEGER NOT NULL CHECK (late_calls >= 0),
            pending_calls INTEGER NOT NULL CHECK (pending_calls >= 0),
            list_amount_micros INTEGER NOT NULL CHECK (list_amount_micros >= 0),
            amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
            payable_fen INTEGER NOT NULL CHECK (payable_fen >= 0),
            UNIQUE(project_id, month)
        );
        CREATE TABLE IF NOT EXISTS billing_statement_lines (
            statement_id TEXT NOT NULL REFERENCES billing_statements(id),
            task_id TEXT NOT NULL UNIQUE,
            key_id TEXT NOT NULL,
            model TEXT NOT NULL,
            source_month TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('usage', 'late_usage')),
            settled_at TEXT NOT NULL,
            output_tokens INTEGER CHECK (output_tokens >= 0),
            price_id INTEGER,
            terms_id INTEGER,
            unit_price_micros_per_million INTEGER CHECK (unit_price_micros_per_million >= 0),
            payable_bps INTEGER CHECK (payable_bps BETWEEN 0 AND 10000),
            list_amount_micros INTEGER NOT NULL CHECK (list_amount_micros >= 0),
            amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0)
        );
        CREATE INDEX IF NOT EXISTS billing_statement_lines_in_order
            ON billing_statement_lines(statement_id, settled_at, task_id);
        CREATE INDEX IF NOT EXISTS video_charges_statement_scope
            ON video_charges(project_id, datetime(settled_at), task_id)
            WHERE status IN ('priced', 'pending');
    """)


def _month_start(month: str) -> date:
    if not isinstance(month, str) or not MONTH.fullmatch(month):
        raise StatementError("invalid_month", "请输入 YYYY-MM 格式的账期")
    return date.fromisoformat(month + "-01")


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


def _beijing_month_start(now: datetime) -> date:
    return now.astimezone(BEIJING).date().replace(day=1)


def _utc_boundary(start: date) -> str:
    return datetime.combine(start, time.min, BEIJING).astimezone(UTC).strftime(SQLITE_DATETIME)


def _next_month(start: date) -> date:
    return date(start.year + (start.month == 12), start.month % 12 + 1, 1)


def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
    if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise StatementError("project_not_found", "项目不存在")


def _validate_page(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1:
        raise StatementError("invalid_pagination", "页码和每页条数须为正整数")


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.rollback()


def _oldest_routed_month(connection: sqlite3.Connection, project_id: str, month_start: date) -> date:
    previous = month_start
    for row in connection.execute("""
        SELECT month FROM billing_statements WHERE project_id = ? AND month < ? ORDER BY month DESC
    """, (project_id, month_start.isoformat()[:7])):
        expected = (previous - timedelta(days=1)).replace(day=1)
        if row["month"] != expected.isoformat()[:7]:
            break
        previous = expected
    return previous


def _scope(connection: sqlite3.Connection, project_id: str, month_start: date) -> tuple[str, str, str]:
    oldest = _oldest_routed_month(connection, project_id, month_start)
    return (project_id, _utc_boundary(oldest), _utc_boundary(_next_month(month_start)))


def _live_totals(connection: sqlite3.Connection, month_start: date, scope: tuple[str, str, str]) -> _Totals:
    usage_calls = late_calls = pending_calls = list_amount = amount = 0
    rows = connection.execute("""
        SELECT charge.status, charge.list_amount_micros, charge.amount_micros,
            datetime(charge.settled_at) < ? AS late
    """ + LIVE_SCOPE, (_utc_boundary(month_start), *scope))
    for row in rows:
        if row["status"] == "pending":
            pending_calls += 1
            continue
        if row["late"]:
            late_calls += 1
        else:
            usage_calls += 1
        list_amount += row["list_amount_micros"]
        amount += row["amount_micros"]
    # The amount is nonnegative. Round the entire statement from micros to fen half up once.
    payable_fen = (amount + MICROS_PER_FEN // 2) // MICROS_PER_FEN
    return _Totals(usage_calls, late_calls, pending_calls, list_amount, amount, payable_fen)


def _money(value: int | None, decimals: int = 6) -> str | None:
    if value is None:
        return None
    yuan, digits = divmod(value, 10**decimals)
    return f"{yuan}.{digits:0{decimals}d}"


def _item(row: sqlite3.Row) -> dict:
    return {
        "taskId": row["task_id"], "model": row["model"], "sourceMonth": row["source_month"],
        "kind": row["kind"], "settledAt": row["settled_at"], "outputTokens": row["output_tokens"],
        "priceVersion": row["price_id"], "termsVersion": row["terms_id"],
        "unitPrice": _money(row["unit_price_micros_per_million"]), "payableBps": row["payable_bps"],
        "listAmount": _money(row["list_amount_micros"]), "amount": _money(row["amount_micros"]),
    }


def _payload(
    month: str, totals: _Totals, rows: list[sqlite3.Row], *, page: int, page_size: int,
    statement_id: str | None = None, confirmed_at: str | None = None,
) -> dict:
    return {
        "id": statement_id,
        "month": month,
        "status": "confirmed" if statement_id is not None else "estimate",
        "currency": "CNY",
        "confirmedAt": confirmed_at,
        "summary": {
            "usageCalls": totals.usage_calls, "lateCalls": totals.late_calls, "pendingCalls": totals.pending_calls,
            "listAmount": _money(totals.list_amount_micros), "amount": _money(totals.amount_micros),
            "payableAmount": _money(totals.payable_fen, 2),
        },
        "pagination": {"page": page, "pageSize": page_size, "total": totals.usage_calls + totals.late_calls},
        "items": [_item(row) for row in rows],
    }


def _frozen_payload(connection: sqlite3.Connection, statement: sqlite3.Row, page: int, page_size: int) -> dict:
    rows = connection.execute("""
        SELECT * FROM billing_statement_lines WHERE statement_id = ?
        ORDER BY settled_at, task_id LIMIT ? OFFSET ?
    """, (statement["id"], page_size, (page - 1) * page_size)).fetchall()
    totals = _Totals(
        statement["usage_calls"], statement["late_calls"], statement["pending_calls"],
        statement["list_amount_micros"], statement["amount_micros"], statement["payable_fen"],
    )
    return _payload(
        statement["month"], totals, rows, page=page, page_size=page_size,
        statement_id=statement["id"], confirmed_at=statement["confirmed_at"],
    )


def _find_month(connection: sqlite3.Connection, project_id: str, month: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM billing_statements WHERE project_id = ? AND month = ?", (project_id, month),
    ).fetchone()


def _find_statement(connection: sqlite3.Connection, project_id: str, statement_id: str) -> sqlite3.Row:
    statement = connection.execute(
        "SELECT * FROM billing_statements WHERE project_id = ? AND id = ?", (project_id, statement_id),
    ).fetchone()
    if statement is None:
        raise StatementError("statement_not_found", "账单不存在")
    return statement


def month_statement(
    connection: sqlite3.Connection, project_id: str, month: str, *,
    page: int = 1, page_size: int = 50, now: datetime | None = None,
) -> dict:
    _validate_page(page, page_size)
    month_start = _month_start(month)
    if month_start > _beijing_month_start(_clock(now)):
        raise StatementError("future_month", "账期尚未开始")
    with _read_snapshot(connection):
        _require_project(connection, project_id)
        existing = _find_month(connection, project_id, month)
        if existing is not None:
            return _frozen_payload(connection, existing, page, page_size)

        scope = _scope(connection, project_id, month_start)
        totals = _live_totals(connection, month_start, scope)
        rows = connection.execute(
            LIVE_LINES + LIVE_SCOPE + " AND charge.status = 'priced' ORDER BY settled_at, task_id LIMIT ? OFFSET ?",
            (_utc_boundary(month_start), *scope, page_size, (page - 1) * page_size),
        ).fetchall()
    return _payload(month, totals, rows, page=page, page_size=page_size)


def confirmed_statement(
    connection: sqlite3.Connection, project_id: str, statement_id: str, *, page: int = 1, page_size: int = 50,
) -> dict:
    _validate_page(page, page_size)
    statement = _find_statement(connection, project_id, statement_id)
    return _frozen_payload(connection, statement, page, page_size)


def confirm_statement(
    connection: sqlite3.Connection, project_id: str, month: str, *, now: datetime | None = None,
) -> dict:
    month_start = _month_start(month)
    now = _clock(now)
    current_month = _beijing_month_start(now)
    if month_start > current_month:
        raise StatementError("future_month", "账期尚未开始")
    if month_start == current_month:
        raise StatementError("cannot_confirm_open_month", "账期尚未结束")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    _require_project(connection, project_id)
    existing = _find_month(connection, project_id, month)
    if existing is not None:
        return _frozen_payload(connection, existing, 1, 50)

    scope = _scope(connection, project_id, month_start)
    totals = _live_totals(connection, month_start, scope)
    if max(totals.list_amount_micros, totals.amount_micros) > SQLITE_MAX_INTEGER:
        raise StatementError("statement_amount_overflow", "账单金额超过系统支持范围")
    statement_id = f"stmt_{uuid4().hex}"
    statement = connection.execute("""
        INSERT INTO billing_statements (
            id, project_id, month, confirmed_at, usage_calls, late_calls, pending_calls,
            list_amount_micros, amount_micros, payable_fen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *
    """, (
        statement_id, project_id, month, now.strftime(SQLITE_DATETIME),
        totals.usage_calls, totals.late_calls, totals.pending_calls,
        totals.list_amount_micros, totals.amount_micros, totals.payable_fen,
    )).fetchone()
    connection.execute("""
        INSERT INTO billing_statement_lines (
            statement_id, task_id, key_id, model, source_month, kind, settled_at, output_tokens, price_id, terms_id,
            unit_price_micros_per_million, payable_bps, list_amount_micros, amount_micros
        ) SELECT ?, lines.* FROM (
    """ + LIVE_LINES + LIVE_SCOPE + " AND charge.status = 'priced') AS lines", (
        statement_id, _utc_boundary(month_start), *scope,
    ))
    return _frozen_payload(connection, statement, 1, 50)


def confirmed_export(connection: sqlite3.Connection, project_id: str, statement_id: str) -> dict:
    statement = _find_statement(connection, project_id, statement_id)
    return _frozen_payload(connection, statement, 1, max(1, statement["usage_calls"] + statement["late_calls"]))
