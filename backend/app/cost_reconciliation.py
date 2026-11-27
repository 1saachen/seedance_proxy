from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, localcontext
import json
import re
import sqlite3

from . import provider_costs
from .billing import MAX_TOKENS
from .general_models import IMAGE_MODELS, TEXT_MODELS
from .model_api import SEEDANCE_MODELS
from .statements import BEIJING, _money


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
TOKEN_UNITS = {
    "token": 1, "tokens": 1, "ktoken": 1_000, "ktokens": 1_000, "千token": 1_000, "千tokens": 1_000,
    "mtoken": 1_000_000, "mtokens": 1_000_000, "百万token": 1_000_000, "百万tokens": 1_000_000,
}
IMAGE_UNITS = {"张": 1, "image": 1, "images": 1}
TEXT_DIMENSIONS = {
    "text_input_tokens", "text_uncached_input_tokens", "text_cached_input_tokens", "text_output_tokens",
    "text_total_tokens",
}
IMAGE_DIMENSIONS = {"image_input_count", "image_output_count", "image_output_count_le_2_61m"}
IMAGE_EXTRA_DIMENSIONS = {
    "doubao-seedream-5-5-261028": {
        "image_output_count_2_61m_to_4_63m", "image_output_count_4_63m_to_8_30m",
    },
    "doubao-seedream-5-0-pro-260628": {"image_additional_input_count", "image_output_count_gt_2_61m"},
}
IMAGE_SIZE = re.compile(r"([1-9][0-9]{0,5})x([1-9][0-9]{0,5})", re.ASCII)


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


def _customer_totals(connection: sqlite3.Connection, month: str, project_id: str | None = None) -> dict:
    query = "SELECT payable_fen FROM billing_statements WHERE month = ?"
    parameters = (month,)
    if project_id is not None:
        query += " AND project_id = ?"
        parameters += (project_id,)
    rows = connection.execute(query, parameters).fetchall()
    yuan, fen = divmod(sum(row["payable_fen"] for row in rows), 100)
    return {"currency": "CNY", "statementCount": len(rows), "confirmedPayableAmount": f"{yuan}.{fen:02d}"}


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=BEIJING)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=BEIJING)
    return start.astimezone(UTC), end.astimezone(UTC)


def _relay_rows(connection: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    start, end = _month_bounds(month)
    return connection.execute("""
        SELECT task.id, task.project_id, task.upstream_model, task.upstream_id, task.settled_at, task.output_tokens,
            charge.status AS customer_billing_status, charge.amount_micros AS customer_amount_micros
        FROM video_tasks AS task
        LEFT JOIN video_charges AS charge ON charge.task_id = task.id AND charge.project_id = task.project_id
        WHERE task.status = 'succeeded' AND datetime(task.settled_at) >= datetime(?) AND datetime(task.settled_at) < datetime(?)
        ORDER BY task.id
    """, (start.isoformat(), end.isoformat())).fetchall()


def _general_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(general_model_calls)")}


def _general_rows(connection: sqlite3.Connection, month: str, columns: set[str]) -> list[sqlite3.Row]:
    if not columns:
        return []
    start, end = _month_bounds(month)
    bounds = (int(start.timestamp()), int(end.timestamp()))
    fields = ("provider_request_id", "cached_prompt_tokens", "input_images", "image_sizes_json")
    optional = ", ".join(field if field in columns else f"NULL AS {field}" for field in fields)
    settled = "settled_at" if "settled_at" in columns else "NULL"
    return connection.execute(f"""
        SELECT id, project_id, upstream_model, modality, status, {settled} AS settled_at,
            prompt_tokens, completion_tokens, total_tokens, generated_images, {optional}
        FROM general_model_calls
        WHERE status IN ('succeeded', 'uncertain', 'in_progress')
        AND (({settled} >= ? AND {settled} < ?) OR ({settled} IS NULL AND created_at >= ? AND created_at < ?))
        ORDER BY id
    """, (*bounds, *bounds)).fetchall()


def _general_models(connection: sqlite3.Connection, columns: set[str]) -> dict[str, str]:
    models = {upstream: kind for kind, catalog in (("text", TEXT_MODELS), ("image", IMAGE_MODELS))
              for upstream in catalog.values()}
    if columns:
        for row in connection.execute("SELECT DISTINCT upstream_model, modality FROM general_model_calls"):
            models.setdefault(row["upstream_model"], row["modality"])
    return models


def _model_mapping(
    connection: sqlite3.Connection, model_map: dict[str, str] | None, general_models: dict[str, str],
) -> dict[str, str]:
    known = {model.upstream_id for model in SEEDANCE_MODELS.values()}
    known.update(general_models)
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


def _usage_line_reason(line: sqlite3.Row, month: str) -> str | None:
    business_month, category, mode = line["business_month"], line["bill_category"], line["billing_mode"]
    if business_month is not None and business_month != month:
        return "different_business_period"
    if (category is not None and category not in USAGE_CATEGORIES) or (mode is not None and mode not in ON_DEMAND_MODES):
        return "not_usage_line"
    if None in (business_month, category, mode):
        return "unverified_usage_dimensions"
    return None


def _provider_usage(line: sqlite3.Row, units: dict[str, int]) -> tuple[int | None, str | None]:
    multiplier = units.get("".join((line["unit"] or "").casefold().split()))
    if multiplier is None:
        return None, "unsupported_usage_unit"
    if line["usage_count"] is None:
        return None, "missing_usage"
    with localcontext() as context:
        context.prec = 110
        value = Decimal(line["usage_count"]) * multiplier
        if value < 0 or value != value.to_integral_value():
            return None, "missing_usage"
    return int(value), None


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
        model = model_mapping.get(line["config_name"])
        reason = _usage_line_reason(line, month)
        if reason is not None:
            if reason == "unverified_usage_dimensions":
                if model is None:
                    unknown_provider_model = True
                else:
                    incomplete_provider_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, reason))
            continue
        if model is None:
            unknown_provider_model = True
            unmatched_provider.append(_provider_unmatched(line, "unmapped_model"))
            continue
        count, reason = _provider_usage(line, {"token": 1, "tokens": 1})
        if reason is not None:
            incomplete_provider_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, reason))
            continue
        provider_models[model].append((line, count))

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


def _dimensions_for(model: str, kind: str) -> set[str]:
    if kind == "text":
        return TEXT_DIMENSIONS
    return IMAGE_DIMENSIONS | IMAGE_EXTRA_DIMENSIONS.get(model, set())


def _dimension_mapping(data: dict | None, models: dict[str, str]) -> dict[tuple[str, str], str]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("dimension map must be a JSON object keyed by upstream model")
    mapping = {}
    for model, codes in data.items():
        if model not in models or not isinstance(codes, dict):
            raise ValueError("dimension map must name a known text or image model and map its ElementCode values")
        supported = _dimensions_for(model, models[model])
        for code, dimension in codes.items():
            if (not isinstance(code, str) or not code.isprintable() or code.strip() != code or not code
                    or not isinstance(dimension, str) or dimension not in supported):
                raise ValueError("dimension map contains an invalid ElementCode or a dimension incompatible with its model")
            mapping[(model, code)] = dimension
    return mapping


def _known_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else None


def _image_pixels(size_data: str | None, generated: int | None) -> list[int] | None:
    if size_data is None or generated is None:
        return None
    try:
        sizes = json.loads(size_data)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(sizes, list) or len(sizes) != generated:
        return None
    pixels = []
    for size in sizes:
        match = IMAGE_SIZE.fullmatch(size) if isinstance(size, str) else None
        if match is None:
            return None
        width, height = map(int, match.groups())
        count = width * height
        if count > 8_300_000:
            return None
        pixels.append(count)
    return pixels


def _general_counts(row: sqlite3.Row) -> dict[str, int | None]:
    if row["modality"] == "text":
        prompt = _known_count(row["prompt_tokens"])
        cached = _known_count(row["cached_prompt_tokens"])
        if prompt is not None and cached is not None and cached > prompt:
            cached = None
        return {
            "text_input_tokens": prompt,
            "text_uncached_input_tokens": prompt - cached if prompt is not None and cached is not None else None,
            "text_cached_input_tokens": cached,
            "text_output_tokens": _known_count(row["completion_tokens"]),
            "text_total_tokens": _known_count(row["total_tokens"]),
        }

    inputs = _known_count(row["input_images"])
    generated = _known_count(row["generated_images"])
    pixels = _image_pixels(row["image_sizes_json"], generated)
    return {
        "image_input_count": inputs,
        "image_additional_input_count": max(0, inputs - 1) if inputs is not None else None,
        "image_output_count": generated,
        "image_output_count_le_2_61m": sum(size <= 2_610_000 for size in pixels) if pixels is not None else None,
        "image_output_count_2_61m_to_4_63m": sum(2_610_000 < size <= 4_630_000 for size in pixels)
        if pixels is not None else None,
        "image_output_count_4_63m_to_8_30m": sum(4_630_000 < size <= 8_300_000 for size in pixels)
        if pixels is not None else None,
        "image_output_count_gt_2_61m": sum(size > 2_610_000 for size in pixels) if pixels is not None else None,
    }


def _general_unmatched(row: sqlite3.Row, reason: str, dimension: str | None = None) -> dict:
    return {
        "requestId": row["id"], "projectId": row["project_id"], "upstreamModel": row["upstream_model"],
        "modality": row["modality"], "settledAt": row["settled_at"], "dimension": dimension, "reason": reason,
    }


def _general_report(
    lines: list[sqlite3.Row], calls: list[sqlite3.Row], month: str, mapping: dict[str, str],
    models: dict[str, str], dimension_mapping: dict[tuple[str, str], str], unknown_provider_model: bool,
) -> tuple[list[dict], list[dict], list[dict]]:
    provider_rows = defaultdict(list)
    unmatched_provider = []
    incomplete_models = set()
    incomplete_dimensions = set()
    for line in lines:
        model = mapping[line["config_name"]]
        reason = _usage_line_reason(line, month)
        if reason is not None:
            if reason == "unverified_usage_dimensions":
                incomplete_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, reason))
            continue
        dimension = dimension_mapping.get((model, line["element_code"]))
        if dimension is None:
            incomplete_models.add(model)
            unmatched_provider.append(_provider_unmatched(line, "unverified_usage_dimension"))
            continue
        units = TOKEN_UNITS if models[model] == "text" else IMAGE_UNITS
        count, reason = _provider_usage(line, units)
        if reason is not None:
            incomplete_dimensions.add((model, dimension))
            unmatched_provider.append(_provider_unmatched(line, reason))
            continue
        provider_rows[(model, dimension)].append((line, count))

    ready_calls = defaultdict(list)
    unmatched_relay = []
    incomplete_local_models = set()
    compared_models = {model for model, _ in provider_rows}
    for call in calls:
        model = call["upstream_model"]
        if call["status"] != "succeeded" or call["settled_at"] is None:
            incomplete_local_models.add(model)
            reason = "local_result_unconfirmed" if call["status"] != "succeeded" else "missing_settlement_time"
            unmatched_relay.append(_general_unmatched(call, reason))
        elif model not in compared_models:
            unmatched_relay.append(_general_unmatched(call, "no_provider_usage"))
        else:
            ready_calls[model].append((call, _general_counts(call)))

    comparisons = []
    for (model, dimension), rows in sorted(provider_rows.items()):
        local = ready_calls[model]
        if not local:
            unmatched_provider.extend(_provider_unmatched(row, "no_local_usage") for row, _ in rows)
            continue
        known = []
        for call, counts in local:
            count = counts.get(dimension)
            if count is None:
                unmatched_relay.append(_general_unmatched(call, "missing_usage", dimension))
            else:
                known.append(count)
        missing = len(local) - len(known)
        provider_count = sum(count for _, count in rows)
        local_count = sum(known) if known else None
        complete = not (missing or unknown_provider_model or model in incomplete_models
                        or model in incomplete_local_models or (model, dimension) in incomplete_dimensions)
        comparisons.append({
            "upstreamModel": model, "modality": models[model], "dimension": dimension,
            "unit": "token" if models[model] == "text" else "image",
            "providerLineCount": len(rows), "relayRequestCount": len(local), "unknownRelayUsageRequests": missing,
            "providerCount": provider_count, "relayKnownCount": local_count,
            "difference": provider_count - local_count if complete and local_count is not None else None,
        })
    return comparisons, unmatched_provider, unmatched_relay


def _correlation_mapping(data: dict, source: dict, lines: list[sqlite3.Row]) -> dict[str, tuple[str, str]]:
    if not isinstance(data, dict) or set(data) != {"scope", "lines"} or data["scope"] != source:
        raise ValueError("correlation map scope must exactly match the active bill import and selected scope")
    entries = data["lines"]
    if not isinstance(entries, dict):
        raise ValueError("correlation map lines must be an object keyed by BillDetailId")
    line_ids = {line["bill_detail_id"] for line in lines}
    mapping = {}
    for line_id, locator in entries.items():
        if line_id not in line_ids:
            raise ValueError("correlation map contains a BillDetailId outside the active import")
        if not isinstance(locator, dict) or len(locator) != 1:
            raise ValueError("each correlation must specify exactly one project or request identifier")
        field, identifier = next(iter(locator.items()))
        if (field not in {"projectId", "localRequestId", "providerRequestId", "providerTaskId"}
                or not isinstance(identifier, str) or identifier == "-" or not provider_costs.IDENTIFIER.fullmatch(identifier)):
            raise ValueError("correlation map contains an invalid project or request identifier")
        mapping[line_id] = (field, identifier)
    return mapping


def _attribution_report(
    connection: sqlite3.Connection, lines: list[sqlite3.Row], video_rows: list[sqlite3.Row],
    general_rows: list[sqlite3.Row], source: dict, model_mapping: dict[str, str], correlation_map: dict,
) -> dict:
    mapping = _correlation_mapping(correlation_map, source, lines)
    month = source["month"]
    projects = {row[0] for row in connection.execute("SELECT id FROM projects")}
    requests = defaultdict(list)
    for modality, rows, provider_ref, column in (
        ("video", video_rows, "providerTaskId", "upstream_id"),
        (None, general_rows, "providerRequestId", "provider_request_id"),
    ):
        for row in rows:
            if modality is None and (row["settled_at"] is None or row["status"] != "succeeded"):
                continue
            entry = (modality or row["modality"], row)
            requests[("localRequestId", row["id"])].append(entry)
            if row[column]:
                requests[(provider_ref, row[column])].append(entry)

    attributed_lines = []
    unresolved = []
    unresolved_lines = []
    by_project = defaultdict(list)
    by_request = defaultdict(list)
    request_details = {}
    for line in lines:
        line_id = line["bill_detail_id"]
        match = mapping.get(line_id)
        reason = _usage_line_reason(line, month)
        project_id = request_id = None
        if reason is None and match is None:
            reason = "no_verified_attribution"
        if reason is None:
            reference, identifier = match
            if reference == "projectId":
                if identifier in projects:
                    project_id = identifier
                else:
                    reason = "mapped_project_not_found"
            else:
                entries = requests.get(match, ())
                if len(entries) != 1:
                    reason = "mapped_request_ambiguous" if entries else "mapped_request_not_found_in_scope"
                else:
                    modality, row = entries[0]
                    model = model_mapping.get(line["config_name"])
                    if model is None or model != row["upstream_model"]:
                        reason = "mapped_request_model_mismatch"
                    else:
                        project_id, request_id = row["project_id"], row["id"]
                        if modality == "video":
                            status = row["customer_billing_status"] or "not_recorded"
                            amount = _money(row["customer_amount_micros"])
                        else:
                            status, amount = "unbilled", None
                        request_details[(project_id, request_id)] = {
                            "modality": modality, "customer": {"currency": "CNY", "status": status, "amount": amount},
                        }
        if reason is not None:
            unresolved.append(_provider_unmatched(line, reason))
            unresolved_lines.append(line)
            continue
        attributed_lines.append({
            "billDetailId": line_id, "projectId": project_id, "localRequestId": request_id, "matchedBy": reference,
        })
        by_project[project_id].append(line)
        if request_id is not None:
            by_request[(project_id, request_id)].append(line)

    return {
        "method": "operator_verified_bill_detail_map",
        "attributedProviderLines": attributed_lines,
        "unmatchedProviderLines": unresolved,
        "unattributedProvider": _provider_totals(unresolved_lines),
        "projects": [
            {"projectId": project_id, "attributedProvider": _provider_totals(rows),
             "customers": _customer_totals(connection, month, project_id)}
            for project_id, rows in sorted(by_project.items())
        ],
        "requests": [
            {"projectId": project_id, "localRequestId": request_id,
             **request_details[(project_id, request_id)], "attributedProvider": _provider_totals(rows)}
            for (project_id, request_id), rows in sorted(by_request.items())
        ],
    }


def build_report(
    connection: sqlite3.Connection, *, payer_id: str, owner_id: str, product: str, month: str,
    confirm_usage_scope: bool = False, model_map: dict[str, str] | None = None,
    dimension_map: dict | None = None, correlation_map: dict | None = None,
) -> dict:
    header, lines = provider_costs.active_lines(
        connection, payer_id=payer_id, owner_id=owner_id, product=product, month=month,
    )
    if model_map is not None and not confirm_usage_scope:
        raise ValueError("model map requires explicit confirmation of matching usage scope")
    if (dimension_map is not None or correlation_map is not None) and not confirm_usage_scope:
        raise ValueError("dimension and correlation maps require explicit confirmation of matching usage scope")
    source = {
        "importId": header["id"], "month": header["month"], "payerId": header["payer_id"],
        "ownerId": header["owner_id"], "product": header["product"],
    }
    video_rows = _relay_rows(connection, month)
    columns = _general_columns(connection)
    general_rows = _general_rows(connection, month, columns)
    known_general = _general_models(connection, columns) if confirm_usage_scope else {}
    mapping = _model_mapping(connection, model_map, known_general) if confirm_usage_scope else {}
    dimensions = _dimension_mapping(dimension_map, known_general)
    video_lines, general_lines = [], []
    for line in lines:
        bucket = general_lines if mapping.get(line["config_name"]) in known_general else video_lines
        bucket.append(line)
    usage = _usage_report(video_lines, video_rows, month, confirm_usage_scope, mapping)
    if general_lines or general_rows or dimension_map is not None:
        if confirm_usage_scope:
            unknown_provider_model = any(
                row["reason"] in {"unmapped_model", "unverified_usage_dimensions"}
                and mapping.get(row["configName"]) is None for row in usage["unmatchedProviderLines"]
            )
            comparisons, unmatched_lines, unmatched_calls = _general_report(
                general_lines, general_rows, month, mapping, known_general, dimensions, unknown_provider_model,
            )
            usage["unmatchedProviderLines"] = sorted(
                [*usage["unmatchedProviderLines"], *unmatched_lines], key=lambda row: row["billDetailId"],
            )
        else:
            comparisons = []
            unmatched_calls = [_general_unmatched(row, "scope_unconfirmed") for row in general_rows]
        usage.update(generalModelComparisons=comparisons, unmatchedRelayCalls=unmatched_calls)

    report = {
        "source": source,
        "provider": _provider_totals(lines),
        "customers": _customer_totals(connection, month),
        "usage": usage,
    }
    if correlation_map is not None:
        report["attribution"] = _attribution_report(
            connection, lines, video_rows, general_rows, source, mapping, correlation_map,
        )
    return report
