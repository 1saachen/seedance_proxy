from datetime import UTC, datetime
from decimal import Decimal
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import accounts, billing, statements, usage
from .admin_auth import CurrentAdmin, record_audit
from .model_api import SEEDANCE_MODELS


router = APIRouter(prefix="/api/admin", tags=["Admin operations"])
Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=100)]
PRICE_MODELS = {model.upstream_id: (name, model.video.resolutions) for name, model in SEEDANCE_MODELS.items()}
PROJECT_SEARCH = """
    FROM projects AS project
    LEFT JOIN customer_accounts AS customer ON customer.project_id = project.id
    WHERE instr(project.id, ?) > 0 OR instr(customer.email, ?) > 0
"""
PRICE_COLUMNS = """
    id, upstream_model AS upstreamModel, resolution, contains_video_input AS containsVideoInput,
    strftime('%Y-%m-%dT%H:%M:%SZ', effective_at, 'unixepoch') AS effectiveAt,
    unit_price_micros_per_million, source
"""
TERMS_COLUMNS = """
    id, strftime('%Y-%m-%dT%H:%M:%SZ', effective_at, 'unixepoch') AS effectiveAt, enabled, payable_bps AS payableBps
"""


class StatementConfirmation(BaseModel):
    expected_payable_amount: str = Field(alias="expectedPayableAmount", pattern=r"^(0|[1-9][0-9]{0,15})\.[0-9]{2}$")


class TermsChange(BaseModel):
    effective_at: str = Field(alias="effectiveAt", max_length=40)
    enabled: bool = Field(strict=True)
    payable_bps: int = Field(alias="payableBps", ge=0, le=billing.BASIS_POINTS, strict=True)


class PriceChange(BaseModel):
    upstream_model: str = Field(alias="upstreamModel", min_length=1, max_length=128)
    resolution: str = Field(max_length=5)
    contains_video_input: bool = Field(alias="containsVideoInput", strict=True)
    effective_at: str = Field(alias="effectiveAt", max_length=40)
    unit_price: str = Field(alias="unitPrice", pattern=r"^[0-9]{1,7}(\.[0-9]{1,6})?$")
    source: str = Field(min_length=1, max_length=200)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _statement_error(error: statements.StatementError) -> HTTPException:
    status = 404 if error.code == "project_not_found" else 422
    return _error(status, error.code, str(error))


def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
    if connection.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise _error(404, "project_not_found", "项目不存在")


def _effective_at(value: str) -> int:
    try:
        timestamp = datetime.fromisoformat(value)
        offset = timestamp.utcoffset()
        if offset is not None and not offset.microseconds and not timestamp.microsecond:
            seconds = int(timestamp.astimezone(UTC).timestamp())
            if seconds >= 0:
                return seconds
    except (ValueError, OverflowError):
        pass
    raise _error(422, "invalid_effective_at", "请输入包含时区的生效时间，精确到秒")


def _terms_payload(row: sqlite3.Row) -> dict:
    return {**dict(row), "enabled": bool(row["enabled"])}


def _price_payload(row: sqlite3.Row) -> dict:
    payload = dict(row)
    yuan, fraction = divmod(payload.pop("unit_price_micros_per_million"), 1_000_000)
    payload["unitPrice"] = f"{yuan}.{fraction:06d}"
    payload["containsVideoInput"] = bool(payload["containsVideoInput"])
    return payload


@router.get("/projects")
def projects(
    request: Request, _: CurrentAdmin, q: Annotated[str, Query(max_length=254)] = "",
    page: Page = 1, page_size: PageSize = 20,
) -> dict:
    query = q.strip().lower()
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) " + PROJECT_SEARCH, (query, query)).fetchone()[0]
        rows = connection.execute(
            """
            SELECT project.id, project.created_at AS createdAt, customer.id AS customerId, customer.email,
                (SELECT COUNT(*) FROM business_keys WHERE project_id = project.id AND revoked_at IS NULL) AS activeKeys,
                (SELECT COUNT(*) FROM business_keys WHERE project_id = project.id AND revoked_at IS NOT NULL) AS revokedKeys
            """
            + PROJECT_SEARCH + " ORDER BY project.created_at DESC, project.id DESC LIMIT ? OFFSET ?",
            (query, query, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": total, "page": page, "pageSize": page_size}


@router.post("/projects", status_code=201)
def create_customer(credentials: accounts.Credentials, request: Request, admin: CurrentAdmin) -> dict:
    email, password = accounts.parse_credentials(credentials)
    with request.app.state.db.connect() as connection:
        customer, created_at = accounts.provision_customer(connection, email, password)
        record_audit(connection, admin, "customer.create", "project", customer.project_id, {"customerId": customer.id})
    return {
        "id": customer.project_id, "customerId": customer.id, "email": customer.email, "createdAt": created_at,
        "activeKeys": 0, "revokedKeys": 0,
    }


@router.get("/projects/{project_id}/keys")
def project_keys(project_id: str, request: Request, _: CurrentAdmin) -> dict:
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        _require_project(connection, project_id)
        return {"keys": accounts.project_keys(connection, project_id)}


@router.get("/projects/{project_id}/usage")
def project_usage(
    project_id: str, request: Request, _: CurrentAdmin,
    month: Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])$")] = None,
    page: Page = 1, page_size: PageSize = 20,
) -> dict:
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        _require_project(connection, project_id)
        return usage.project_usage(
            connection, project_id, today=datetime.now(usage.SHANGHAI).date(), month=month, page=page,
            page_size=page_size, include_pending_count=True,
        )


@router.get("/projects/{project_id}/statements/{month}")
def project_statement(
    project_id: str, month: str, request: Request, _: CurrentAdmin, page: Page = 1, page_size: PageSize = 50,
) -> dict:
    try:
        with request.app.state.db.connect() as connection:
            return statements.month_statement(connection, project_id, month, page=page, page_size=page_size)
    except statements.StatementError as error:
        raise _statement_error(error) from None


@router.post("/projects/{project_id}/statements/{month}/confirm")
def confirm_statement(
    project_id: str, month: str, request: Request, admin: CurrentAdmin, payload: StatementConfirmation | None = None,
) -> dict:
    try:
        with request.app.state.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM billing_statements WHERE project_id = ? AND month = ?", (project_id, month),
            ).fetchone()
            now = datetime.now(statements.BEIJING)
            if existing is None and payload is not None and month < now.strftime("%Y-%m"):
                preview = statements.month_statement(connection, project_id, month, page_size=1, now=now)
                if preview["summary"]["payableAmount"] != payload.expected_payable_amount:
                    raise _error(409, "statement_amount_changed", "应付金额已更新，请重新查看账单后再确认")
            statement = statements.confirm_statement(connection, project_id, month, now=now)
            if existing is None:
                record_audit(connection, admin, "billing.statement.confirm", "statement", statement["id"], {
                    "projectId": project_id, "month": month, "payableAmount": statement["summary"]["payableAmount"],
                })
            return statement
    except statements.StatementError as error:
        raise _statement_error(error) from None


@router.get("/projects/{project_id}/billing-terms")
def billing_terms(project_id: str, request: Request, _: CurrentAdmin) -> dict:
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        _require_project(connection, project_id)
        rows = connection.execute(
            f"SELECT {TERMS_COLUMNS} FROM project_billing_terms WHERE project_id = ? ORDER BY effective_at DESC",
            (project_id,),
        ).fetchall()
    return {"items": [_terms_payload(row) for row in rows]}


@router.post("/projects/{project_id}/billing-terms")
def add_billing_terms(project_id: str, payload: TermsChange, request: Request, admin: CurrentAdmin) -> dict:
    effective_at = _effective_at(payload.effective_at)
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_project(connection, project_id)
        existing = connection.execute(
            "SELECT 1 FROM project_billing_terms WHERE project_id = ? AND effective_at = ?", (project_id, effective_at),
        ).fetchone()
        try:
            version = billing.add_project_terms(
                connection, project_id=project_id, effective_at=effective_at, enabled=payload.enabled,
                payable_bps=payload.payable_bps,
            )
        except ValueError:
            raise _error(409, "billing_terms_conflict", "同一生效时间已有不同的计费条款") from None
        terms = _terms_payload(connection.execute(
            f"SELECT {TERMS_COLUMNS} FROM project_billing_terms WHERE id = ?", (version,),
        ).fetchone())
        if existing is None:
            record_audit(connection, admin, "billing.terms.create", "project", project_id, {
                "termsVersion": version, "effectiveAt": terms["effectiveAt"], "enabled": payload.enabled,
                "payableBps": payload.payable_bps,
            })
        return terms


@router.get("/prices")
def prices(request: Request, _: CurrentAdmin, page: Page = 1, page_size: PageSize = 50) -> dict:
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) FROM video_prices").fetchone()[0]
        rows = connection.execute(
            f"SELECT {PRICE_COLUMNS} FROM video_prices ORDER BY effective_at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [_price_payload(row) for row in rows],
        "models": [
            {"id": model_id, "label": name, "resolutions": resolutions}
            for model_id, (name, resolutions) in PRICE_MODELS.items()
        ],
        "total": total, "page": page, "pageSize": page_size,
    }


@router.post("/prices")
def add_price(payload: PriceChange, request: Request, admin: CurrentAdmin) -> dict:
    effective_at = _effective_at(payload.effective_at)
    if payload.upstream_model not in PRICE_MODELS or payload.resolution not in PRICE_MODELS[payload.upstream_model][1]:
        raise _error(422, "invalid_price_model", "请选择目录中支持该分辨率的模型版本")
    unit_price = int(Decimal(payload.unit_price).scaleb(6))
    if unit_price > billing.MAX_UNIT_PRICE or not payload.source.strip().isprintable():
        raise _error(422, "invalid_price", "请输入支持范围内的单价和有效的价目来源")
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT 1 FROM video_prices
            WHERE upstream_model = ? AND resolution = ? AND contains_video_input = ? AND effective_at = ?
            """, (payload.upstream_model, payload.resolution, payload.contains_video_input, effective_at),
        ).fetchone()
        try:
            version = billing.add_price(
                connection, upstream_model=payload.upstream_model, resolution=payload.resolution,
                contains_video_input=payload.contains_video_input, effective_at=effective_at,
                unit_price_micros_per_million=unit_price, source=payload.source,
            )
        except ValueError:
            raise _error(409, "price_conflict", "同一模型计价档位和生效时间已有不同价目") from None
        price = _price_payload(connection.execute(
            f"SELECT {PRICE_COLUMNS} FROM video_prices WHERE id = ?", (version,),
        ).fetchone())
        if existing is None:
            record_audit(connection, admin, "billing.price.create", "price", str(version), {
                field: price[field]
                for field in ("upstreamModel", "resolution", "containsVideoInput", "effectiveAt", "unitPrice")
            })
        return price
