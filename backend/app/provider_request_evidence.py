from collections import Counter, defaultdict
from datetime import datetime
import re
import sqlite3

from .statements import BEIJING


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_METRICS_BY_KIND = {
    "video": ("completionTokens",),
    "text": ("promptTokens", "cachedPromptTokens", "completionTokens"),
    "image": ("inputImages", "generatedImages"),
}
_QUERY_FIELDS = {
    "promptTokens": "InputTokens", "cachedPromptTokens": "CacheHitTokens", "completionTokens": "OutputTokens",
    "inputImages": "InputImageCount", "generatedImages": "ImageNumber",
}
_LOG_FIELDS = {
    "promptTokens": "prompt_tokens", "completionTokens": "completion_tokens",
    "inputImages": "input_images", "generatedImages": "generated_images",
}
_LOG_KEYS = {"request_id", "X-Client-Request-Id", "account_id", "model_name", "usage"}
_MAX_EVIDENCE_ROWS = 20_000


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid provider evidence identifier")
    return value


def _belongs_to_owner(value: object, owner_id: str) -> bool:
    return type(value) in (str, int) and str(value) == owner_id


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _period(month: str) -> tuple[int, int]:
    if re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None:
        raise ValueError("invalid provider evidence month")
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=BEIJING)
    end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=BEIJING)
    return int(start.timestamp()), int(end.timestamp())


def _usage_responses(responses: list, owner_id: str) -> tuple[dict[str, list[dict]], int]:
    result = {}
    empty = 0
    received = 0
    for envelope in responses:
        metadata = envelope.get("ResponseMetadata") if isinstance(envelope, dict) else None
        payload = envelope.get("Result") if isinstance(envelope, dict) else None
        if not isinstance(metadata, dict) or metadata.get("Action") != "GetInferenceRequestUsage" or metadata.get("Error"):
            raise ValueError("expected successful GetInferenceRequestUsage responses")
        records = payload.get("Records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise ValueError("invalid provider request usage records")
        received += len(records)
        if received > _MAX_EVIDENCE_ROWS:
            raise ValueError("too many provider request usage records")
        if not records:
            empty += 1
            continue
        for record in records:
            if not isinstance(record, dict) or not _belongs_to_owner(record.get("AccountID"), owner_id):
                raise ValueError("provider request usage account does not match the report scope")
        ids = {_identifier(record.get("RequestID")) for record in records}
        if len(ids) != 1:
            raise ValueError("each provider usage response must belong to one request")
        request_id = ids.pop()
        if request_id in result and result[request_id] != records:
            raise ValueError("conflicting provider usage responses for the same request")
        result[request_id] = records
    return result, empty


def _invocation_logs(logs: list, owner_id: str) -> dict[str, list[dict]]:
    result = defaultdict(list)
    for row in logs:
        if not isinstance(row, dict) or row.keys() - _LOG_KEYS:
            raise ValueError("use only the allowlisted invocation log fields")
        if not _belongs_to_owner(row.get("account_id"), owner_id):
            raise ValueError("provider invocation log account does not match the report scope")
        request_id = _identifier(row.get("request_id"))
        _identifier(row.get("X-Client-Request-Id"))
        _identifier(row.get("model_name"))
        usage = row.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or usage.keys() - (set(_LOG_FIELDS.values()) | {"prompt_tokens_details"}):
                raise ValueError("use only the allowlisted invocation usage fields")
            details = usage.get("prompt_tokens_details")
            if details is not None and (not isinstance(details, dict) or details.keys() - {"cached_tokens"}):
                raise ValueError("use only cached_tokens in invocation prompt token details")
        if len(result[request_id]) < 2 and row not in result[request_id]:
            result[request_id].append(row)
    return result


def _local_candidates(connection: sqlite3.Connection, provider_id: str, local_id: str | None) -> list[sqlite3.Row]:
    general = connection.execute("""
        SELECT id, project_id, key_id, modality AS kind, model, upstream_model, status, created_at, settled_at,
               provider_request_id,
               prompt_tokens, cached_prompt_tokens, completion_tokens, input_images, generated_images
        FROM general_model_calls WHERE id = ? OR provider_request_id = ?
    """, (local_id, provider_id)).fetchall()
    if local_id is None:
        return general
    video = connection.execute("""
        SELECT id, project_id, key_id, 'video' AS kind, model, upstream_model, status,
               CAST(strftime('%s', created_at) AS INTEGER) AS created_at,
               CAST(strftime('%s', settled_at) AS INTEGER) AS settled_at,
               NULL AS provider_request_id,
               NULL AS prompt_tokens, NULL AS cached_prompt_tokens, output_tokens AS completion_tokens,
               NULL AS input_images, NULL AS generated_images
        FROM video_tasks WHERE id = ?
    """, (local_id,)).fetchall()
    return general + video


def _model_matches(record: dict, row: sqlite3.Row) -> bool:
    upstream = row["upstream_model"]
    name = record.get("ModelName")
    version = record.get("ModelVersion")
    if upstream is None:
        return False
    if name == upstream:
        return version in (None, "", upstream.rsplit("-", 1)[-1])
    base, separator, suffix = upstream.rpartition("-")
    return bool(separator and name in (base, row["model"]) and version == suffix)


def _query_count(records: list[dict], metric: str) -> int | None:
    values = [_count(row.get(_QUERY_FIELDS[metric])) for row in records]
    return sum(values) if all(value is not None for value in values) else None


def _log_count(log: dict, metric: str) -> int | None:
    reported = log.get("usage") or {}
    if metric == "cachedPromptTokens":
        return _count((reported.get("prompt_tokens_details") or {}).get("cached_tokens"))
    return _count(reported.get(_LOG_FIELDS[metric]))


def _compare(row: sqlite3.Row, records: list[dict], log: dict | None) -> dict:
    local_fields = {
        "promptTokens": "prompt_tokens", "cachedPromptTokens": "cached_prompt_tokens",
        "completionTokens": "completion_tokens", "inputImages": "input_images", "generatedImages": "generated_images",
    }
    comparisons = {}
    for metric in _METRICS_BY_KIND[row["kind"]]:
        measured = _query_count(records, metric) if records else _log_count(log or {}, metric)
        local = row[local_fields[metric]]
        comparisons[metric] = {
            "provider": measured, "relay": local,
            "difference": measured - local if measured is not None and local is not None else None,
        }
    return {
        "requestId": row["id"], "projectId": row["project_id"], "keyId": row["key_id"],
        "upstreamModel": row["upstream_model"], "kind": row["kind"], "comparisons": comparisons,
    }


def build_report(
    connection: sqlite3.Connection, evidence: object, *, payer_id: str, owner_id: str, product: str, month: str,
) -> dict:
    scope = {"payerId": payer_id, "ownerId": owner_id, "product": product, "month": month}
    if not isinstance(evidence, dict) or evidence.keys() - {"scope", "usageResponses", "invocationLogs"}:
        raise ValueError("invalid provider request evidence document")
    if evidence.get("scope") != scope:
        raise ValueError("provider request evidence scope does not match the report")
    responses = evidence.get("usageResponses", [])
    invocation_logs = evidence.get("invocationLogs", [])
    if not isinstance(responses, list) or not isinstance(invocation_logs, list):
        raise ValueError("provider request evidence entries must be arrays")
    if len(responses) + len(invocation_logs) > _MAX_EVIDENCE_ROWS:
        raise ValueError("too many provider request evidence entries")

    start, end = _period(month)
    usage, empty = _usage_responses(responses, owner_id)
    logs = _invocation_logs(invocation_logs, owner_id)
    matched = []
    unmatched = []
    candidates = {}
    local_counts = Counter()
    for provider_id in sorted(usage.keys() | logs.keys()):
        records = usage.get(provider_id, [])
        log_rows = logs.get(provider_id, [])
        log = log_rows[0] if len(log_rows) == 1 else None
        local_id = log["X-Client-Request-Id"] if log is not None else None
        source = "request_usage" if records else "invocation_log"
        rows = _local_candidates(connection, provider_id, local_id)
        reason = None
        row = rows[0] if len(rows) == 1 else None
        conflicting_identity = row is not None and (
            local_id not in (None, row["id"]) or (records and row["provider_request_id"] not in (None, provider_id))
        )
        if len(log_rows) > 1 or len(rows) > 1 or conflicting_identity:
            reason = "ambiguous_request_identity"
        elif row is None:
            reason = "no_exact_local_request"
        elif row["status"] != "succeeded":
            reason = "local_result_not_successful"
        elif not start <= (row["settled_at"] or row["created_at"] or 0) < end:
            reason = "different_month"
        elif (log is not None and log["model_name"] != row["upstream_model"]) or any(
            not _model_matches(record, row) for record in records
        ):
            reason = "model_mismatch"
        elif any(record.get("BillingStatus") != "normal" for record in records):
            reason = "provider_usage_not_settled"
        elif any(not start <= (_count(record.get("Timestamp")) or 0) < end for record in records):
            reason = "different_provider_month"
        if reason is not None:
            item = {"providerRequestId": provider_id, "source": source, "reason": reason}
            if row is not None and reason != "ambiguous_request_identity":
                item.update(requestId=row["id"], projectId=row["project_id"])
            unmatched.append(item)
        else:
            candidates[provider_id] = {"providerRequestId": provider_id, "source": source, **_compare(row, records, log)}
            local_counts[candidates[provider_id]["requestId"]] += 1

    project_counts = Counter()
    for provider_id, item in candidates.items():
        if local_counts[item["requestId"]] > 1:
            unmatched.append({"providerRequestId": provider_id, "source": item["source"], "reason": "ambiguous_request_identity"})
            continue
        matched.append(item)
        project_counts[item["projectId"]] += 1
    return {
        "scope": scope, "matched": matched, "unmatched": sorted(unmatched, key=lambda item: item["providerRequestId"]),
        "projects": [{"projectId": project, "matchedRequests": count} for project, count in sorted(project_counts.items())],
        "emptyUsageResponses": empty,
    }
