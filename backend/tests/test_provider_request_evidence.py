from datetime import datetime

import pytest

from app import accounts, general_models, model_api, provider_request_evidence
from app.db import Database
from app.statements import BEIJING


SCOPE = {"payer_id": "71001", "owner_id": "73002", "product": "ark", "month": "2026-11"}
SCOPE_PAYLOAD = {"payerId": "71001", "ownerId": "73002", "product": "ark", "month": "2026-11"}
OBSERVED = int(datetime(2026, 11, 6, 12, tzinfo=BEIJING).timestamp())
TEXT_MODEL = "doubao-seed-2-1-lite-260915"
IMAGE_MODEL = "doubao-seedream-5-5-261028"
VIDEO_MODEL = "doubao-seedance-2-5-260628"


@pytest.fixture
def ledger(tmp_path):
    database = Database(tmp_path / "evidence.sqlite")
    with database.connect() as connection:
        accounts.initialize(connection)
        model_api.initialize(connection)
        general_models.initialize(connection)
        for number in (1, 2):
            connection.execute("INSERT INTO projects VALUES (?, '2026-11-01T00:00:00Z')", (f"project-{number}",))
            connection.execute("""
                INSERT INTO business_keys (id, project_id, name, prefix, secret_hash, created_at)
                VALUES (?, ?, 'Fixture', 'fixture', ?, '2026-11-01T00:00:00Z')
            """, (f"key-{number}", f"project-{number}", f"fixture-hash-{number}"))
        connection.executemany("""
            INSERT INTO general_model_calls (
                id, project_id, key_id, modality, model, upstream_model, provider_request_id, status,
                created_at, settled_at, prompt_tokens, cached_prompt_tokens, completion_tokens, input_images, generated_images
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?)
        """, (
            ("chat-local", "project-1", "key-1", "text", "doubao-seed-2.1-lite", TEXT_MODEL, "chat-ark",
             OBSERVED, OBSERVED, 110, 4, 22, None, None),
            ("image-local", "project-2", "key-2", "image", "doubao-seedream-5.5", IMAGE_MODEL, None,
             OBSERVED, OBSERVED, None, None, None, 1, 1),
        ))
        connection.execute("""
            INSERT INTO video_tasks (id, project_id, key_id, request_hash, model, upstream_model, status,
                contains_video_input, output_tokens, created_at, settled_at)
            VALUES ('video-local', 'project-2', 'key-2', 'fixture', 'doubao-seedance-2.5', ?, 'succeeded', 0, 400,
                '2026-11-06T04:00:00Z', '2026-11-06T04:01:00Z')
        """, (VIDEO_MODEL,))
        yield connection


def _record(request_id="chat-ark", **changes):
    return {"AccountID": 73002, "RequestID": request_id, "Timestamp": OBSERVED, "ModelName": "doubao-seed-2.1-lite",
            "ModelVersion": "260915", "BillingStatus": "normal", "InputTokens": 100, "OutputTokens": 21,
            "CacheHitTokens": 4, **changes}


def _envelope(*records):
    return {"ResponseMetadata": {"Action": "GetInferenceRequestUsage"}, "Result": {"Records": list(records)}}


def _log(request_id, local_id, model, **usage):
    return {"request_id": request_id, "X-Client-Request-Id": local_id, "account_id": "73002", "model_name": model,
            "usage": usage}


def test_exact_request_evidence_matches_three_modalities_and_groups_local_customers(ledger):
    text = _envelope(_record(), _record(InputTokens=10, OutputTokens=2, CacheHitTokens=0))
    evidence = {
        "scope": SCOPE_PAYLOAD, "usageResponses": [text, text, _envelope(), _envelope(_record("untracked"))],
        "invocationLogs": [
            _log("image-ark", "image-local", IMAGE_MODEL, input_images=1, generated_images=2),
            _log("video-ark", "video-local", VIDEO_MODEL, completion_tokens=400),
        ],
    }
    report = provider_request_evidence.build_report(ledger, evidence, **SCOPE)
    text, image, video = report["matched"]
    assert text["requestId"] == "chat-local" and text["source"] == "request_usage"
    assert text["comparisons"] == {
        "promptTokens": {"provider": 110, "relay": 110, "difference": 0},
        "cachedPromptTokens": {"provider": 4, "relay": 4, "difference": 0},
        "completionTokens": {"provider": 23, "relay": 22, "difference": 1},
    }
    assert image["source"] == video["source"] == "invocation_log"
    assert image["requestId"] == "image-local" and image["comparisons"]["generatedImages"]["difference"] == 1
    assert video["requestId"] == "video-local" and video["comparisons"]["completionTokens"]["difference"] == 0
    assert report["unmatched"] == [{"providerRequestId": "untracked", "source": "request_usage", "reason": "no_exact_local_request"}]
    assert report["projects"] == [{"projectId": "project-1", "matchedRequests": 1}, {"projectId": "project-2", "matchedRequests": 2}]
    assert report["emptyUsageResponses"] == 1


def test_conflicting_identity_and_model_are_unmatched(ledger):
    report = provider_request_evidence.build_report(ledger, {
        "scope": SCOPE_PAYLOAD, "usageResponses": [_envelope(_record())], "invocationLogs": [
            _log("chat-ark", "image-local", IMAGE_MODEL, input_images=1),
            _log("video-ark", "video-local", IMAGE_MODEL, generated_images=1),
        ],
    }, **SCOPE)
    assert report["matched"] == []
    assert {item["providerRequestId"]: item["reason"] for item in report["unmatched"]} == {
        "chat-ark": "ambiguous_request_identity", "video-ark": "model_mismatch",
    }


def test_evidence_rejects_other_accounts_and_unredacted_logs(ledger):
    mismatched = {"scope": SCOPE_PAYLOAD, "usageResponses": [_envelope(_record(AccountID=73003))]}
    with pytest.raises(ValueError, match="account does not match"):
        provider_request_evidence.build_report(ledger, mismatched, **SCOPE)
    unredacted = {"scope": SCOPE_PAYLOAD, "invocationLogs": [
        {**_log("image-ark", "image-local", IMAGE_MODEL), "input": "sample prompt"},
    ]}
    with pytest.raises(ValueError, match="allowlisted invocation log fields"):
        provider_request_evidence.build_report(ledger, unredacted, **SCOPE)
