import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NamedTuple
from uuid import uuid4


EXPORT_ACTION = "ListBillDetail"
MONTH = re.compile(r"2[0-9]{3}-(0[1-9]|1[0-2])")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")
DECIMAL_STRING = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
MAX_DECIMAL_PLACES = 40


class ProviderBillError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class BillLine(NamedTuple):
    bill_detail_id: str
    business_month: str | None
    config_name: str | None
    bill_category: str | None
    billing_mode: str | None
    unit: str | None
    usage_count: str | None
    deduction_count: str | None
    instance_no: str | None
    resource_id: str | None
    element_code: str | None
    original_amount: str
    discount_amount: str
    discounted_amount: str
    payable_amount: str
    cash_paid_amount: str | None
    coupon_amount: str | None
    round_amount: str | None
    unpaid_amount: str | None
    credit_carried_amount: str | None


@dataclass(frozen=True)
class BillExport:
    payer_id: str
    owner_id: str
    product: str
    month: str
    content_sha256: str
    lines: tuple[BillLine, ...]


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS provider_bill_imports (
            id TEXT PRIMARY KEY,
            payer_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            product TEXT NOT NULL,
            month TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            line_count INTEGER NOT NULL CHECK (line_count >= 0),
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            activated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(payer_id, owner_id, product, month, content_sha256)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS provider_bill_active_scope
            ON provider_bill_imports(payer_id, owner_id, product, month) WHERE active = 1;
        CREATE TABLE IF NOT EXISTS provider_bill_lines (
            import_id TEXT NOT NULL REFERENCES provider_bill_imports(id),
            bill_detail_id TEXT NOT NULL,
            business_month TEXT,
            config_name TEXT,
            bill_category TEXT,
            billing_mode TEXT,
            unit TEXT,
            usage_count TEXT,
            deduction_count TEXT,
            instance_no TEXT,
            resource_id TEXT,
            element_code TEXT,
            original_amount TEXT NOT NULL,
            discount_amount TEXT NOT NULL,
            discounted_amount TEXT NOT NULL,
            payable_amount TEXT NOT NULL,
            cash_paid_amount TEXT,
            coupon_amount TEXT,
            round_amount TEXT,
            unpaid_amount TEXT,
            credit_carried_amount TEXT,
            PRIMARY KEY(import_id, bill_detail_id)
        );
    """)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> None:
    raise ValueError("invalid JSON number")


def _identifier(value: object, field: str, *, allow_integer: bool = False) -> str:
    if allow_integer and type(value) is int:
        value = str(value)
    if not isinstance(value, str) or value == "-" or not IDENTIFIER.fullmatch(value):
        raise ProviderBillError("invalid_export", f"Invalid or missing {field}")
    return value


def _scope(payer_id: str, owner_id: str, product: str, month: str) -> tuple[str, str, str, str]:
    if not isinstance(month, str) or not MONTH.fullmatch(month):
        raise ProviderBillError("invalid_export", "Bill month must use YYYY-MM")
    return (_identifier(payer_id, "payer ID"), _identifier(owner_id, "owner ID"), _identifier(product, "product"), month)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isprintable():
        raise ProviderBillError("invalid_export", f"Invalid {field}")
    value = value.strip()
    return None if value in ("", "-") else value


def _decimal(value: object, field: str, *, required: bool = False) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        if value in ("", "-"):
            value = None
        elif not DECIMAL_STRING.fullmatch(value):
            raise ProviderBillError("invalid_export", f"Invalid decimal {field}")
    if value is None and not required:
        return None
    if type(value) is not int and not isinstance(value, (str, Decimal)):
        raise ProviderBillError("invalid_export", f"Invalid or missing decimal {field}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ProviderBillError("invalid_export", f"Invalid decimal {field}") from None
    if not number.is_finite():
        raise ProviderBillError("invalid_export", f"Invalid decimal {field}")
    if number.is_zero():
        return "0"

    # Normalize insignificant zeros without applying Decimal's ambient rounding context.
    sign, digits, exponent = number.as_tuple()
    size = len(digits)
    while digits[size - 1] == 0:
        size -= 1
    exponent += len(digits) - size
    if exponent < -MAX_DECIMAL_PLACES or size + exponent > MAX_DECIMAL_PLACES:
        raise ProviderBillError("invalid_export", f"Decimal {field} is outside the supported precision range")
    return format(Decimal((sign, digits[:size], exponent)), "f")


def _parse_line(raw: object, scope: tuple[str, str, str, str]) -> BillLine:
    if not isinstance(raw, dict):
        raise ProviderBillError("invalid_export", "Each bill detail must be an object")
    payer = _identifier(raw.get("PayerID"), "PayerID", allow_integer=True)
    owner = _identifier(raw.get("OwnerID"), "OwnerID", allow_integer=True)
    if (payer, owner, raw.get("Product"), raw.get("BillPeriod")) != scope:
        raise ProviderBillError("bill_scope_mismatch", "A bill detail does not match the selected account, product or month")
    if raw.get("Currency") != "CNY" or "CurrencySettlement" in raw and raw["CurrencySettlement"] != "CNY":
        raise ProviderBillError("unsupported_currency", "The export must use CNY for billing and settlement")

    detail_id = _identifier(raw.get("BillDetailId"), "BillDetailId")
    business_month = _optional_text(raw.get("BusiPeriod"), "BusiPeriod")
    if business_month is not None and not MONTH.fullmatch(business_month):
        raise ProviderBillError("invalid_export", "Invalid BusiPeriod")
    billing_mode = raw.get("BillingMode")
    if type(billing_mode) is int:
        billing_mode = str(billing_mode)

    return BillLine(
        bill_detail_id=detail_id,
        business_month=business_month,
        config_name=_optional_text(raw.get("ConfigName"), "ConfigName"),
        bill_category=_optional_text(raw.get("BillCategory"), "BillCategory"),
        billing_mode=_optional_text(billing_mode, "BillingMode"),
        unit=_optional_text(raw.get("Unit"), "Unit"),
        usage_count=_decimal(raw.get("Count"), "Count"),
        deduction_count=_decimal(raw.get("DeductionCount"), "DeductionCount"),
        instance_no=_optional_text(raw.get("InstanceNo"), "InstanceNo"),
        resource_id=_optional_text(raw.get("ResourceID"), "ResourceID"),
        element_code=_optional_text(raw.get("ElementCode"), "ElementCode"),
        original_amount=_decimal(raw.get("OriginalBillAmount"), "OriginalBillAmount", required=True),
        discount_amount=_decimal(raw.get("PreferentialBillAmount"), "PreferentialBillAmount", required=True),
        discounted_amount=_decimal(raw.get("DiscountBillAmount"), "DiscountBillAmount", required=True),
        payable_amount=_decimal(raw.get("PayableAmount"), "PayableAmount", required=True),
        cash_paid_amount=_decimal(raw.get("PaidAmount"), "PaidAmount"),
        coupon_amount=_decimal(raw.get("CouponAmount"), "CouponAmount"),
        round_amount=_decimal(raw.get("RoundAmount"), "RoundAmount"),
        unpaid_amount=_decimal(raw.get("UnpaidAmount"), "UnpaidAmount"),
        credit_carried_amount=_decimal(raw.get("CreditCarriedAmount"), "CreditCarriedAmount"),
    )


def parse_export(text: str, *, payer_id: str, owner_id: str, product: str, month: str) -> BillExport:
    scope = _scope(payer_id, owner_id, product, month)
    if not isinstance(text, str):
        raise ProviderBillError("invalid_export", "The export must be UTF-8 JSON text")
    try:
        payload = json.loads(
            text.removeprefix("\ufeff"), parse_float=Decimal, parse_constant=_reject_nonfinite,
            object_pairs_hook=_unique_json_object,
        )
    except (ValueError, InvalidOperation, RecursionError):
        raise ProviderBillError("invalid_export", "The export must be valid JSON without duplicate fields") from None
    pages = payload if isinstance(payload, list) else [payload]
    if not pages:
        raise ProviderBillError("invalid_export", "At least one official response page is required")

    by_detail_id: dict[str, BillLine] = {}
    expected_total = None
    for page in pages:
        if not isinstance(page, dict):
            raise ProviderBillError("invalid_export", "Each response page must be an object")
        metadata = page.get("ResponseMetadata")
        result = page.get("Result")
        if not isinstance(metadata, dict) or metadata.get("Action") != EXPORT_ACTION or metadata.get("Error"):
            raise ProviderBillError("invalid_export", "Each page must be a successful ListBillDetail response")
        if not isinstance(result, dict) or not isinstance(result.get("List"), list):
            raise ProviderBillError("invalid_export", "Each page must include Result.List")
        if result.get("Warning") not in (None, "", "-"):
            raise ProviderBillError("incomplete_export", "The provider reported a warning about selected bill filters")

        total = result.get("Total")
        if type(total) is not int or total < 0 or (expected_total is not None and total != expected_total):
            raise ProviderBillError("incomplete_export", "All pages must have the same nonnegative Result.Total")
        expected_total = total
        for raw in result["List"]:
            line = _parse_line(raw, scope)
            prior = by_detail_id.setdefault(line.bill_detail_id, line)
            if prior != line:
                raise ProviderBillError("bill_detail_conflict", "Repeated bill detail IDs contain different values")

    if len(by_detail_id) != expected_total:
        raise ProviderBillError("incomplete_export", "The number of unique bill details does not match Result.Total")
    lines = tuple(by_detail_id[detail_id] for detail_id in sorted(by_detail_id))
    content = json.dumps((EXPORT_ACTION, scope, lines), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return BillExport(*scope, digest, lines)


def _active_import(connection: sqlite3.Connection, scope: tuple[str, str, str, str]) -> sqlite3.Row | None:
    return connection.execute("""
        SELECT * FROM provider_bill_imports
        WHERE payer_id = ? AND owner_id = ? AND product = ? AND month = ? AND active = 1
    """, scope).fetchone()


def import_export(connection: sqlite3.Connection, export: BillExport, *, replace: bool = False) -> dict:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    scope = (export.payer_id, export.owner_id, export.product, export.month)
    active = _active_import(connection, scope)
    if active is not None:
        if active["content_sha256"] == export.content_sha256:
            return {"importId": active["id"], "rows": active["line_count"], "disposition": "unchanged"}
        if not replace:
            raise ProviderBillError("bill_snapshot_conflict", "This scope already has a different active bill snapshot")

    prior = connection.execute("""
        SELECT id FROM provider_bill_imports
        WHERE payer_id = ? AND owner_id = ? AND product = ? AND month = ? AND content_sha256 = ?
    """, (*scope, export.content_sha256)).fetchone()
    import_id = prior["id"] if prior is not None else f"billimp_{uuid4().hex}"
    connection.execute("SAVEPOINT import_provider_bill")
    try:
        if active is not None:
            connection.execute("UPDATE provider_bill_imports SET active = 0 WHERE id = ?", (active["id"],))
        if prior is not None:
            connection.execute("""
                UPDATE provider_bill_imports SET active = 1, activated_at = CURRENT_TIMESTAMP WHERE id = ?
            """, (import_id,))
        else:
            connection.execute("""
                INSERT INTO provider_bill_imports (id, payer_id, owner_id, product, month, content_sha256, line_count, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (import_id, *scope, export.content_sha256, len(export.lines)))
            connection.executemany("""
                INSERT INTO provider_bill_lines (
                    import_id, bill_detail_id, business_month, config_name, bill_category, billing_mode, unit,
                    usage_count, deduction_count, instance_no, resource_id, element_code, original_amount,
                    discount_amount, discounted_amount, payable_amount, cash_paid_amount, coupon_amount,
                    round_amount, unpaid_amount, credit_carried_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ((import_id, *line) for line in export.lines))
    except BaseException:
        connection.execute("ROLLBACK TO import_provider_bill")
        raise
    finally:
        connection.execute("RELEASE import_provider_bill")

    return {"importId": import_id, "rows": len(export.lines), "disposition": "replaced" if active is not None else "imported"}


def active_lines(
    connection: sqlite3.Connection, *, payer_id: str, owner_id: str, product: str, month: str,
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    active = _active_import(connection, _scope(payer_id, owner_id, product, month))
    if active is None:
        raise ProviderBillError("bill_not_found", "No bill has been imported for the selected scope")
    rows = connection.execute("""
        SELECT * FROM provider_bill_lines WHERE import_id = ? ORDER BY bill_detail_id
    """, (active["id"],)).fetchall()
    return active, rows
