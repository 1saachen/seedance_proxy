import csv
from datetime import datetime
from io import StringIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import statements
from .accounts import CustomerPrincipal, require_customer


router = APIRouter(prefix="/api/customer/billing", tags=["Customer billing"])
Customer = Annotated[CustomerPrincipal, Depends(require_customer)]
Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=100)]
Month = Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])$")]
CSV_ITEM_FIELDS = (
    "taskId", "model", "sourceMonth", "kind", "settledAt", "outputTokens", "priceVersion",
    "termsVersion", "unitPrice", "payableBps", "listAmount", "amount",
)


def _http_error(error: statements.StatementError) -> HTTPException:
    status = 404 if error.code == "statement_not_found" else 422
    return HTTPException(status, {"code": error.code, "message": str(error)})


@router.get("")
def billing_month(
    request: Request, customer: Customer, month: Month = None, page: Page = 1, page_size: PageSize = 50,
) -> dict:
    month = month or datetime.now(statements.BEIJING).strftime("%Y-%m")
    try:
        with request.app.state.db.connect() as connection:
            connection.execute("BEGIN")
            return statements.month_statement(connection, customer.project_id, month, page=page, page_size=page_size)
    except statements.StatementError as error:
        raise _http_error(error) from None


@router.get("/{statement_id}")
def billing_statement(
    statement_id: str, request: Request, customer: Customer, page: Page = 1, page_size: PageSize = 50,
) -> dict:
    try:
        with request.app.state.db.connect() as connection:
            connection.execute("BEGIN")
            return statements.confirmed_statement(
                connection, customer.project_id, statement_id, page=page, page_size=page_size,
            )
    except statements.StatementError as error:
        raise _http_error(error) from None


def _csv_cell(value: str | int | None) -> str | int | None:
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


@router.get("/{statement_id}/export.csv", response_class=Response)
def export_statement(statement_id: str, request: Request, customer: Customer) -> Response:
    try:
        with request.app.state.db.connect() as connection:
            connection.execute("BEGIN")
            statement = statements.confirmed_export(connection, customer.project_id, statement_id)
    except statements.StatementError as error:
        raise _http_error(error) from None

    body = StringIO(newline="")
    body.write("\ufeff")
    writer = csv.writer(body)
    writer.writerow(("statementId", "month", *CSV_ITEM_FIELDS))
    for item in statement["items"]:
        values = (statement["id"], statement["month"], *(item[field] for field in CSV_ITEM_FIELDS))
        writer.writerow(_csv_cell(value) for value in values)
    filename = f"richi-proxy-billing-{statement['month']}.csv"
    return Response(body.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    })
