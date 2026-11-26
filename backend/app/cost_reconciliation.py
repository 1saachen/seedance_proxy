from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, localcontext
import sqlite3

from . import provider_costs
from .billing import MAX_TOKENS
from .model_api import SEEDANCE_MODELS
from .statements import BEIJING


AMOUNTS = {
    "originalAmount": "original_amount",
    "discountAmount": "discount_amount",
    "discountedAmount": "discounted_amount",
    "payableAmount": "payable_amount",
    "cashPaidAmount": "cash_paid_amount",
    "couponAmount": "coupon_amount",
    "roundAmount": "round_amount",
    "unpaidAmount": "unpaid_amount",
    "creditCarriedAmount": "credit_carried_amount",
}
USAGE_CATEGORIES = {"consume-use", "消费-使用"}
ON_DEMAND_MODES = {"2", "按量计费"}


def _amount_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _provider_totals(lines: list[sqlite3.Row]) -> dict:
    with localcontext() as context:
        context.prec = 110
        totals = {}
        for field, column in AMOUNTS.items():
            values = [line[column] for line in lines]
            totals[field] = None if None in values else sum((Decimal(value) for value in values), Decimal(0))
        payments = [totals[field] for field in ("cashPaidAmount", "creditCarriedAmount", "unpaidAmount")]
        difference = None
        if totals["payableAmount"] is not None and None not in payments:
            difference = totals["payableAmount"] - sum(payments)
    return {
        "currency": "CNY", "lineCount": len(lines),
        **{field: _amount_text(value) for field, value in totals.items()},
        "paymentBalanceDifference": _amount_text(difference),
    }


def _customer_totals(connection: sqlite3.Connection, month: str) -> dict:
    rows = connection.execute("SELECT payable_fen FROM billing_statements WHERE month = ?", (month,)).fetchall()
    yuan, fen = divmod(sum(row["payable_fen"] for row in rows), 100)
    return {"currency": "CNY", "statementCount": len(rows), "confirmedPayableAmount": f"{yuan}.{fen:02d}"}


def _relay_rows(connection: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=BEIJING)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=BEIJING)
    return connection.execute("""
        SELECT id, project_id, upstream_model, settled_at, output_tokens FROM video_tasks
        WHERE status = 'succeeded' AND datetime(settled_at) >= datetime(?) AND datetime(settled_at) < datetime(?)
        ORDER BY id
    """, (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())).fetchall()


def _model_mapping(connection: sqlite3.Connection, model_map: dict[str, str] | None) -> dict[str, str]:
    known = {model.upstream_id for model in SEEDANCE_MODELS.values()}
    known.update(row[0] for row in connection.execute("SELECT DISTINCT upstream_model FROM video_tasks WHERE upstream_model IS NOT NULL"))
    mapping = {model: model for model in known}
    if model_map is None:
        return mapping
    if not isinstance(model_map, dict):
        raise ValueError("model map must be a JSON object")
    for name, model in model_map.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(model, str) or model not in known:
            raise ValueError("each model map entry must name a known upstream model version")
        name = name.strip()
        if name in mapping and mapping[name] != model:
            raise ValueError("a model name cannot be mapped to different upstream versions")
        mapping[name] = model
    return mapping


def _provider_unmatched(line: sqlite3.Row, reason: str) -> dict:
    return {
        "billDetailId": line["bill_detail_id"], "businessMonth": line["business_month"],
        "configName": line["config_name"], "billingMode": line["billing_mode"], "billCategory": line["bill_category"],
        "instanceNo": line["instance_no"], "resourceId": line["resource_id"], "elementCode": line["element_code"],
        "unit": line["unit"], "usageCount": line["usage_count"], "deductionCount": line["deduction_count"],
        "payableAmount": line["payable_amount"], "cashPaidAmount": line["cash_paid_amount"], "reason": reason,
    }


def _relay_tokens(row: sqlite3.Row) -> int | None:
    tokens = row["output_tokens"]
    return tokens if type(tokens) is int and 0 <= tokens <= MAX_TOKENS else None


def _relay_unmatched(row: sqlite3.Row, reason: str) -> dict:
    return {
        "taskId": row["id"], "projectId": row["project_id"], "upstreamModel": row["upstream_model"],
        "settledAt": row["settled_at"], "outputTokens": _relay_tokens(row), "reason": reason,
    }


def _usage_report(
    lines: list[sqlite3.Row], tasks: list[sqlite3.Row], month: str, scope_confirmed: bool, model_mapping: dict[str, str],
) -> dict:
    if not scope_confirmed:
        return {
            "scopeConfirmed": False, "comparisons": [],
            "unmatchedProviderLines": [_provider_unmatched(row, "scope_unconfirmed") for row in lines],
            "unmatchedRelayTasks": [_relay_unmatched(row, "scope_unconfirmed") for row in tasks],
        }

    provider_models: dict[str, list[tuple[sqlite3.Row, int]]] = defaultdict(list)
    unmatched_provider = []
    incomplete_provider_models: set[str] = set()
    unknown_provider_model = False
    for line in lines:
        business_month, category, mode = line["business_month"], line["bill_category"], line["billing_mode"]
        if business_month is not None and business_month != month:
            unmatched_provider.append(_provider_unmatched(line, "different_business_period"))
            continue
        if (category is not None and category not in USAGE_CATEGORIES) or (mode is not None and mode not in ON_DEMAND_MODES):
            unmatched_provider.append(_provider_unmatched(line, "not_usage_line"))
            continue
        model = model_mapping.get(line["config_name"])
        if None in (business_month, category, mode):
            if model is None:
                unknown_provider_model = True
            else:
                incomplete_provider_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, "unverified_usage_dimensions"))
            continue
        if model is None:
            unknown_provider_model = True
            unmatched_provider.append(_provider_unmatched(line, "unmapped_model"))
            continue
        if (line["unit"] or "").casefold() not in {"token", "tokens"}:
            incomplete_provider_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, "unsupported_usage_unit"))
            continue
        value = Decimal(line["usage_count"]) if line["usage_count"] is not None else None
        if value is None or value < 0 or value != value.to_integral_value():
            incomplete_provider_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, "missing_usage"))
            continue
        provider_models[model].append((line, int(value)))

    relay_models: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unmatched_relay = []
    unknown_relay_model = False
    for row in tasks:
        if not row["upstream_model"]:
            unknown_relay_model = True
            unmatched_relay.append(_relay_unmatched(row, "missing_upstream_model"))
        elif row["upstream_model"] not in provider_models:
            unmatched_relay.append(_relay_unmatched(row, "no_provider_usage"))
        else:
            relay_models[row["upstream_model"]].append(row)

    comparisons = []
    for model, provider_rows in sorted(provider_models.items()):
        local_rows = relay_models.get(model)
        if local_rows is None:
            unmatched_provider.extend(_provider_unmatched(row, "no_local_usage") for row, _ in provider_rows)
            continue
        known_tokens = []
        for row in local_rows:
            tokens = _relay_tokens(row)
            if tokens is None:
                unmatched_relay.append(_relay_unmatched(row, "missing_usage"))
            else:
                known_tokens.append(tokens)
        missing = len(local_rows) - len(known_tokens)
        provider_tokens = sum(tokens for _, tokens in provider_rows)
        relay_known = sum(known_tokens) if known_tokens else None
        complete = not (missing or unknown_provider_model or unknown_relay_model or model in incomplete_provider_models)
        comparisons.append({
            "upstreamModel": model, "providerLineCount": len(provider_rows), "relayTaskCount": len(local_rows),
            "providerTokens": provider_tokens, "relayKnownTokens": relay_known, "unknownRelayUsageTasks": missing,
            "tokenDifference": provider_tokens - relay_known if complete and relay_known is not None else None,
        })
    return {
        "scopeConfirmed": True, "comparisons": comparisons,
        "unmatchedProviderLines": sorted(unmatched_provider, key=lambda row: row["billDetailId"]),
        "unmatchedRelayTasks": sorted(unmatched_relay, key=lambda row: row["taskId"]),
    }


def build_report(
    connection: sqlite3.Connection, *, payer_id: str, owner_id: str, product: str, month: str,
    confirm_usage_scope: bool = False, model_map: dict[str, str] | None = None,
) -> dict:
    header, lines = provider_costs.active_lines(
        connection, payer_id=payer_id, owner_id=owner_id, product=product, month=month,
    )
    if model_map is not None and not confirm_usage_scope:
        raise ValueError("model map requires explicit confirmation of matching usage scope")
    mapping = _model_mapping(connection, model_map) if confirm_usage_scope else {}
    return {
        "source": {
            "importId": header["id"], "month": header["month"], "payerId": header["payer_id"],
            "ownerId": header["owner_id"], "product": header["product"],
        },
        "provider": _provider_totals(lines),
        "customers": _customer_totals(connection, month),
        "usage": _usage_report(lines, _relay_rows(connection, month), month, confirm_usage_scope, mapping),
    }
