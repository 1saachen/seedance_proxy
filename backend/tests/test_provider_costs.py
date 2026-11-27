from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app import billing, cost_reconciliation, general_models, provider_costs, statements
from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.main import create_app


MONTH = "2025-02"
MODEL = "doubao-seedance-2.5"
UPSTREAM_MODEL = "doubao-seedance-2-5-260628"
PAYER = "1000000001"
OWNER = "1000000002"
PRODUCT = "ark"
SCOPE = {"payer_id": PAYER, "owner_id": OWNER, "product": PRODUCT, "month": MONTH}
TEXT_MODEL = "doubao-seed-2.1-lite"
TEXT_UPSTREAM = general_models.TEXT_MODELS[TEXT_MODEL]
IMAGE_MODEL = "doubao-seedream-5.5"
IMAGE_UPSTREAM = general_models.IMAGE_MODELS[IMAGE_MODEL]
PRO_IMAGE_MODEL = "doubao-seedream-5.0-pro"
PRO_IMAGE_UPSTREAM = general_models.IMAGE_MODELS[PRO_IMAGE_MODEL]
START = int(datetime.fromisoformat(f"{MONTH}-01T00:00:00+08:00").timestamp())


@dataclass(frozen=True)
class Customer:
    project_id: str
    key_id: str
    session: dict[str, str]


def _client(path: Path) -> TestClient:
    def no_provider_calls(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Cost reconciliation tests must not call upstream ({request.method})")

    mock = httpx.MockTransport(no_provider_calls)
    return TestClient(create_app(Settings(
        database_path=path, registration_enabled=True, cookie_secure=False,
        ark_api_key=None, ark_asset_access_key=None, ark_asset_secret_key=None,
        video_poll_seconds=0, upload_directory=None, upload_public_origin="", upload_cleanup_seconds=0,
    ), ark_transport=mock, ark_asset_transport=mock))


def _customer(client: TestClient, name: str = "alice") -> Customer:
    client.cookies.clear()
    response = client.post("/api/customer/register", json={"email": f"{name}@example.com", "password": "Example-only-pass-42"})
    assert response.status_code == 201, response.text
    account = response.json()
    created = client.post(
        "/api/customer/keys", headers={"X-CSRF-Token": account["csrfToken"]}, json={"name": "cost reconciliation test"},
    )
    assert created.status_code == 201
    session = {"Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}"}
    client.cookies.clear()
    return Customer(account["customer"]["projectId"], created.json()["key"]["id"], session)


def _price_customer(connection: sqlite3.Connection, customer: Customer) -> None:
    billing.add_price(
        connection, upstream_model=UPSTREAM_MODEL, resolution="720p", contains_video_input=False,
        effective_at=0, unit_price_micros_per_million=1_000_000, source="Synthetic RP-203 price",
    )
    billing.add_project_terms(connection, project_id=customer.project_id, effective_at=0, enabled=True, payable_bps=10000)


def _task(
    connection: sqlite3.Connection, customer: Customer, task_id: str, *,
    tokens: int | None, model: str = MODEL, upstream_model: str | None = UPSTREAM_MODEL,
    settled_at: str = "2025-02-12 00:00:00", status: str = "succeeded",
) -> None:
    row = connection.execute("""
        INSERT INTO video_tasks (
            id, project_id, key_id, request_hash, model, upstream_model, upstream_id, status, result_json,
            requested_resolution, contains_video_input, output_tokens, created_at, settled_at
        ) VALUES (?, ?, ?, 'synthetic', ?, ?, ?, ?, '{"resolution":"720p"}', '720p', 0, ?, '2025-01-01 00:00:00', ?)
        RETURNING *
    """, (task_id, customer.project_id, customer.key_id, model, upstream_model, f"provider-{task_id}", status, tokens, settled_at)).fetchone()
    billing.record_task(connection, row)


def _line(line_id: str = "sample-line-001", **changes) -> dict:
    return {
        "BillDetailId": line_id, "BillPeriod": MONTH, "PayerID": int(PAYER), "OwnerID": int(OWNER),
        "Product": PRODUCT, "Currency": "CNY", "CurrencySettlement": "CNY", "BusiPeriod": MONTH,
        "ConfigName": UPSTREAM_MODEL, "BillCategory": "consume-use", "BillingMode": "按量计费", "Unit": "Token", "Count": "1700",
        "OriginalBillAmount": "10.1234567890", "PreferentialBillAmount": "1.0234567890",
        "DiscountBillAmount": "9.1000000000", "PayableAmount": "9.05", "PaidAmount": "8.947",
        "CouponAmount": "0.101", "RoundAmount": "0.001", "UnpaidAmount": "0", "CreditCarriedAmount": "0.001",
        **changes,
    }


def _call(
    connection: sqlite3.Connection, customer: Customer, local_id: str, *, model: str = TEXT_MODEL,
    prompt: int | None = None, cached: int | None = None, completion: int | None = None,
    inputs: int | None = None, generated: int | None = None, sizes: list[str] | None = None,
    settled_at: int | None = START, status: str = "succeeded", provider_id: str | None = None,
) -> None:
    modality = "text" if model in general_models.TEXT_MODELS else "image"
    upstream = (general_models.TEXT_MODELS if modality == "text" else general_models.IMAGE_MODELS)[model]
    total = prompt + completion if prompt is not None and completion is not None else None
    connection.execute("""
        INSERT INTO general_model_calls (
            id, project_id, key_id, modality, model, upstream_model, status, created_at, settled_at,
            prompt_tokens, cached_prompt_tokens, completion_tokens, total_tokens, input_images, generated_images,
            image_sizes_json, provider_request_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (local_id, customer.project_id, customer.key_id, modality, model, upstream, status, START, settled_at,
          prompt, cached, completion, total, inputs, generated, json.dumps(sizes) if sizes is not None else None,
          provider_id))


def _envelope(*lines: dict, total: int | None = None) -> dict:
    return {
        "ResponseMetadata": {"Action": "ListBillDetail"},
        "Result": {"List": list(lines), "Total": len(lines) if total is None else total},
    }


def _import(connection: sqlite3.Connection, *lines: dict, replace: bool = False) -> dict:
    export = provider_costs.parse_export(json.dumps(_envelope(*lines)), **SCOPE)
    return provider_costs.import_export(connection, export, replace=replace)


def _report(connection: sqlite3.Connection, **options) -> dict:
    return cost_reconciliation.build_report(connection, **SCOPE, **options)


def test_reconciliation_matches_new_video_models_by_exact_upstream_version(tmp_path: Path) -> None:
    variants = {
        "doubao-seedance-2.5-flash": "doubao-seedance-2-5-flash-261115",
        "doubao-seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
        "doubao-seedance-2.0-mini": "doubao-seedance-2-0-mini-260615",
    }
    with _client(tmp_path / "variants.sqlite") as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, customer)
            _import(connection, *(
                _line(f"variant-{index}", ConfigName=version, Count=str(1000 * index))
                for index, version in enumerate(variants.values(), start=1)
            ))
            before_tasks = _report(connection, confirm_usage_scope=True)["usage"]["unmatchedProviderLines"]
            assert len(before_tasks) == len(variants)
            assert {row["reason"] for row in before_tasks} == {"no_local_usage"}
            for index, (alias, version) in enumerate(variants.items(), start=1):
                _task(connection, customer, f"vid_{index:02}", tokens=1000 * index, model=alias, upstream_model=version)
            usage = _report(connection, confirm_usage_scope=True)["usage"]
            assert {row["upstreamModel"]: row["relayKnownTokens"] for row in usage["comparisons"]} == {
                version: 1000 * index for index, version in enumerate(variants.values(), start=1)
            }
            assert all(row["tokenDifference"] == 0 for row in usage["comparisons"])
            assert usage["unmatchedProviderLines"] == []
            assert usage["unmatchedRelayTasks"] == []


def test_snapshot_import_replays_reordered_pages_and_replaces_only_explicitly(tmp_path: Path) -> None:
    with _client(tmp_path / "snapshots.sqlite") as client:
        first = _line()
        second = _line("sample-line-002", Count="300", OriginalBillAmount="0.3000000001")
        with client.app.state.db.connect() as connection:
            imported = _import(connection, first, second)
            assert imported == {"importId": imported["importId"], "rows": 2, "disposition": "imported"}
            equivalent_first = {**first, "OriginalBillAmount": "10.12345678900"}
            pages = [_envelope(second, total=2), _envelope(equivalent_first, total=2)]
            reordered = provider_costs.parse_export(json.dumps(pages, indent=2), **SCOPE)
            same = provider_costs.import_export(connection, reordered)
            assert same == {"importId": imported["importId"], "rows": 2, "disposition": "unchanged"}
            _, active = provider_costs.active_lines(connection, **SCOPE)
            assert len(active) == 2

        modified = {**first, "PayableAmount": "10.00"}
        with pytest.raises(provider_costs.ProviderBillError) as conflict:
            with client.app.state.db.connect() as connection:
                _import(connection, modified, second)
        assert conflict.value.code == "bill_snapshot_conflict"
        with client.app.state.db.connect() as connection:
            assert _report(connection)["source"]["importId"] == imported["importId"]
            replacement = _import(connection, modified, second, replace=True)
            assert replacement["disposition"] == "replaced" and replacement["importId"] != imported["importId"]
            assert _report(connection)["source"]["importId"] == replacement["importId"]
            assert _import(connection, modified, second)["disposition"] == "unchanged"
            restored = _import(connection, first, second, replace=True)
            assert restored == {"importId": imported["importId"], "rows": 2, "disposition": "replaced"}
            assert _report(connection)["source"]["importId"] == imported["importId"]


@pytest.mark.parametrize(
    "changes",
    [
        {"PayerID": 1000000009}, {"OwnerID": 1000000009}, {"Product": "unrelated-product"}, {"BillPeriod": "2025-01"},
        {"Currency": "USD"}, {"CurrencySettlement": "USD"}, {"PayableAmount": "NaN"},
        {"DiscountBillAmount": "Infinity"}, {"OriginalBillAmount": "1,234.56"}, {"PreferentialBillAmount": None},
    ],
)
def test_mixed_snapshot_rejects_invalid_lines_without_replacing_active_import(tmp_path: Path, changes: dict) -> None:
    with _client(tmp_path / "validation.sqlite") as client:
        with client.app.state.db.connect() as connection:
            original = _import(connection, _line())
            expected = _report(connection)
        with pytest.raises(provider_costs.ProviderBillError):
            with client.app.state.db.connect() as connection:
                _import(connection, _line(), _line("sample-line-002", **changes), replace=True)
        with client.app.state.db.connect() as connection:
            assert _report(connection) == expected
            assert expected["source"]["importId"] == original["importId"]


def test_export_requires_complete_official_envelopes_and_unique_detail_ids() -> None:
    wrong_action = _envelope(_line())
    wrong_action["ResponseMetadata"]["Action"] = "GetBillSummary"
    for document in (
        wrong_action, _envelope(_line(), total=2),
        [_envelope(_line(), total=2), _envelope(_line("sample-line-002"), total=3)],
        _envelope(_line(), _line(PaidAmount="22.00")),
    ):
        with pytest.raises(provider_costs.ProviderBillError):
            provider_costs.parse_export(json.dumps(document), **SCOPE)


def test_database_error_during_replacement_keeps_active_snapshot_and_erases_partial_writes(tmp_path: Path) -> None:
    with _client(tmp_path / "rollback.sqlite") as client:
        with client.app.state.db.connect() as connection:
            imported = _import(connection, _line())
            connection.execute("""
                CREATE TRIGGER synthetic_import_failure BEFORE INSERT ON provider_bill_lines
                WHEN NEW.bill_detail_id = 'sample-line-002'
                BEGIN SELECT RAISE(ABORT, 'synthetic import failure'); END
            """)
        with client.app.state.db.connect() as connection:
            with pytest.raises(sqlite3.IntegrityError):
                _import(connection, _line(PaidAmount="8.98"), _line("sample-line-002"), replace=True)
            assert _report(connection)["source"]["importId"] == imported["importId"]
            assert connection.execute("SELECT COUNT(*) FROM provider_bill_imports").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM provider_bill_lines").fetchone()[0] == 1


def test_provider_amounts_keep_official_decimal_values_and_unknown_optional_totals(tmp_path: Path) -> None:
    with _client(tmp_path / "decimals.sqlite") as client:
        with client.app.state.db.connect() as connection:
            _import(connection, _line())
            source = _report(connection)
            assert source["source"] == {
                "importId": source["source"]["importId"], "month": MONTH,
                "payerId": PAYER, "ownerId": OWNER, "product": PRODUCT,
            }
            assert source["provider"] == {
                "currency": "CNY", "lineCount": 1, "originalAmount": "10.123456789", "discountAmount": "1.023456789",
                "discountedAmount": "9.1", "payableAmount": "9.05", "cashPaidAmount": "8.947", "couponAmount": "0.101",
                "roundAmount": "0.001", "unpaidAmount": "0", "creditCarriedAmount": "0.001", "paymentBalanceDifference": "0.102",
            }
            extra = _line(
                "sample-line-002", OriginalBillAmount="-0.0000000001", PreferentialBillAmount="0", DiscountBillAmount="-0.0000000001",
                PayableAmount="-0.0000000001", PaidAmount=None,
            )
            _import(connection, _line(), extra, replace=True)
            current = _report(connection)["provider"]
            assert current["originalAmount"] == "10.1234567889" and current["payableAmount"] == "9.0499999999"
            assert current["cashPaidAmount"] is None and current["paymentBalanceDifference"] is None
            assert current["unpaidAmount"] == "0"


def test_reconciliation_uses_only_verified_model_period_unit_and_usage_dimensions(tmp_path: Path) -> None:
    other_model = "doubao-seedance-2-0-260128"
    model_map = {"Synthetic verified configuration": UPSTREAM_MODEL, "Synthetic unused configuration": other_model}
    comparable_lines = [
        _line(Count="1700"),
        _line("mapped", ConfigName="Synthetic verified configuration", BillCategory="消费-使用", BillingMode="2", Unit="tokens", Count="300"),
        _line("prior-business-period", BusiPeriod="2025-01"),
        _line("bad-category", BillCategory="consume-refund"),
        _line("bad-billing-mode", BillingMode="包年包月"),
        _line("only-provider", ConfigName="Synthetic unused configuration"),
    ]
    incomplete_lines = [
        _line("ambiguous-identifier", ConfigName="Unverified display name", InstanceNo="provider-vid_first", ResourceID="provider-vid_first"),
        _line("non-token-unit", Unit="小时"),
        _line("missing-count", Count=None),
        _line("fractional-token-count", Count="1.25"),
    ]
    with _client(tmp_path / "matching.sqlite") as client:
        alice, bob = _customer(client), _customer(client, "bob")
        with client.app.state.db.connect() as connection:
            _price_customer(connection, alice)
            _task(connection, alice, "vid_first", tokens=1200, settled_at="2025-01-31 16:00:00")
            _task(connection, bob, "vid_second", tokens=700)
            _task(connection, bob, "vid_only_relay", tokens=25, upstream_model="historical-version-with-no-provider-row")
            _task(connection, bob, "vid_previous_month", tokens=999, settled_at="2025-01-31 15:59:59")
            _task(connection, bob, "vid_failed", tokens=999, status="failed")
            _import(connection, *comparable_lines)
            unspecified = _report(connection)
            assert unspecified["usage"]["scopeConfirmed"] is False and unspecified["usage"]["comparisons"] == []
            assert {row["reason"] for row in unspecified["usage"]["unmatchedProviderLines"]} == {"scope_unconfirmed"}
            assert {row["reason"] for row in unspecified["usage"]["unmatchedRelayTasks"]} == {"scope_unconfirmed"}
            with pytest.raises(ValueError):
                _report(connection, model_map=model_map)
            for invalid in ({UPSTREAM_MODEL: other_model}, {f" {UPSTREAM_MODEL} ": other_model}):
                with pytest.raises(ValueError):
                    _report(connection, confirm_usage_scope=True, model_map=invalid)

            report = _report(connection, confirm_usage_scope=True, model_map=model_map)
            usage = report["usage"]
            assert usage["scopeConfirmed"] is True
            assert usage["comparisons"] == [{
                "upstreamModel": UPSTREAM_MODEL, "providerLineCount": 2, "relayTaskCount": 2, "providerTokens": 2000,
                "relayKnownTokens": 1900, "unknownRelayUsageTasks": 0, "tokenDifference": 100,
            }]

            _import(connection, *comparable_lines, _line("unknown-billing-mode", BillingMode=None), replace=True)
            uncertain = _report(connection, confirm_usage_scope=True, model_map=model_map)["usage"]
            assert uncertain["comparisons"][0]["tokenDifference"] is None
            expected_unmatched = {row["billDetailId"]: row["reason"] for row in usage["unmatchedProviderLines"]}
            expected_unmatched["unknown-billing-mode"] = "unverified_usage_dimensions"
            assert {row["billDetailId"]: row["reason"] for row in uncertain["unmatchedProviderLines"]} == expected_unmatched

            _import(connection, *comparable_lines, *incomplete_lines, replace=True)
            usage = _report(connection, confirm_usage_scope=True, model_map=model_map)["usage"]
            assert usage["comparisons"] == [{**report["usage"]["comparisons"][0], "tokenDifference": None}]
            assert {row["billDetailId"]: row["reason"] for row in usage["unmatchedProviderLines"]} == {
                "prior-business-period": "different_business_period", "bad-category": "not_usage_line",
                "bad-billing-mode": "not_usage_line", "ambiguous-identifier": "unmapped_model",
                "non-token-unit": "unsupported_usage_unit", "missing-count": "missing_usage",
                "fractional-token-count": "missing_usage", "only-provider": "no_local_usage",
            }
            assert {row["taskId"]: row["reason"] for row in usage["unmatchedRelayTasks"]} == {
                "vid_only_relay": "no_provider_usage",
            }

            _import(connection, *comparable_lines, replace=True)
            _task(connection, alice, "vid_missing_tokens", tokens=None)
            incomplete = _report(connection, confirm_usage_scope=True, model_map=model_map)["usage"]
            comparison, = incomplete["comparisons"]
            assert comparison["relayTaskCount"] == 3 and comparison["relayKnownTokens"] == 1900
            assert comparison["unknownRelayUsageTasks"] == 1 and comparison["tokenDifference"] is None
            assert {row["taskId"]: row["reason"] for row in incomplete["unmatchedRelayTasks"]}["vid_missing_tokens"] == "missing_usage"
            _task(connection, bob, "vid_missing_version", tokens=10, upstream_model=None)
            unknown_model = _report(connection, confirm_usage_scope=True, model_map=model_map)["usage"]
            assert {row["taskId"]: row["reason"] for row in unknown_model["unmatchedRelayTasks"]}["vid_missing_version"] == "missing_upstream_model"


def test_text_and_image_totals_require_verified_compatible_dimensions(tmp_path: Path) -> None:
    dimensions = {
        TEXT_UPSTREAM: {"input": "text_uncached_input_tokens", "cache": "text_cached_input_tokens",
                        "output": "text_output_tokens"},
        IMAGE_UPSTREAM: {"input": "image_input_count", "small": "image_output_count_le_2_61m",
                         "medium": "image_output_count_2_61m_to_4_63m", "large": "image_output_count_4_63m_to_8_30m"},
        PRO_IMAGE_UPSTREAM: {"input": "image_additional_input_count", "output": "image_output_count_gt_2_61m"},
    }
    with _client(tmp_path / "model-dimensions.sqlite") as client:
        alice = _customer(client)
        with client.app.state.db.connect() as connection:
            _call(connection, alice, "chatcmpl-synthetic", prompt=1000, cached=250, completion=50)
            _call(connection, alice, "chatcmpl-other-period", prompt=500, cached=0, completion=10, settled_at=START - 1)
            for index, size in enumerate(("1500x1500", "2048x2048", "3840x2160")):
                _call(connection, alice, f"img_{index}", model=IMAGE_MODEL, inputs=index, generated=1, sizes=[size])
            _call(connection, alice, "img_pro", model=PRO_IMAGE_MODEL, inputs=3, generated=1, sizes=["2048x2048"])
            lines = [
                _line("text-input", ConfigName="verified text config", ElementCode="input", Unit="千 tokens", Count="0.75"),
                _line("text-cache", ConfigName=TEXT_UPSTREAM, ElementCode="cache", Count="250"),
                _line("text-output", ConfigName=TEXT_UPSTREAM, ElementCode="output", Count="50"),
                _line("image-input", ConfigName=IMAGE_UPSTREAM, ElementCode="input", Unit="张", Count="3"),
                _line("image-small", ConfigName=IMAGE_UPSTREAM, ElementCode="small", Unit="image", Count="1"),
                _line("image-medium", ConfigName=IMAGE_UPSTREAM, ElementCode="medium", Unit="images", Count="1"),
                _line("image-large", ConfigName=IMAGE_UPSTREAM, ElementCode="large", Unit="张", Count="1"),
                _line("pro-additional", ConfigName=PRO_IMAGE_UPSTREAM, ElementCode="input", Unit="张", Count="2"),
                _line("pro-output", ConfigName=PRO_IMAGE_UPSTREAM, ElementCode="output", Unit="张", Count="1"),
            ]
            _import(connection, *lines)
            with pytest.raises(ValueError, match="explicit confirmation"):
                _report(connection, dimension_map=dimensions)
            with pytest.raises(ValueError, match="incompatible"):
                _report(connection, confirm_usage_scope=True, dimension_map={TEXT_UPSTREAM: {"input": "image_input_count"}})

            options = {"confirm_usage_scope": True, "model_map": {"verified text config": TEXT_UPSTREAM}}
            unmapped = _report(connection, **options)["usage"]
            assert unmapped["generalModelComparisons"] == []
            assert {item["reason"] for item in unmapped["unmatchedProviderLines"]} == {"unverified_usage_dimension"}

            usage = _report(connection, **options, dimension_map=dimensions)["usage"]
            assert usage["comparisons"] == [] and usage["unmatchedProviderLines"] == usage["unmatchedRelayCalls"] == []
            comparisons = {item["dimension"]: item for item in usage["generalModelComparisons"]}
            assert len(comparisons) == len(lines)
            assert all(item["difference"] == 0 for item in comparisons.values())
            assert comparisons["text_uncached_input_tokens"]["providerCount"] == 750
            assert comparisons["image_input_count"]["relayRequestCount"] == 3
            assert comparisons["image_additional_input_count"]["relayKnownCount"] == 2

            _call(connection, alice, "chatcmpl-legacy", prompt=100, completion=2, settled_at=None)
            _call(connection, alice, "chatcmpl-missing-cache", prompt=100, completion=0)
            _call(connection, alice, "img-no-sizes", model=IMAGE_MODEL, inputs=0, generated=1)
            _call(connection, alice, "img-outside-bands", model=IMAGE_MODEL, inputs=0, generated=1, sizes=["4000x2500"])
            _import(connection, *lines, _line(
                "wrong-unit", ConfigName=IMAGE_UPSTREAM, ElementCode="small", Unit="Token", Count="1",
            ), replace=True)
            incomplete = _report(connection, **options, dimension_map=dimensions)["usage"]
            comparisons = {item["dimension"]: item for item in incomplete["generalModelComparisons"]}
            assert comparisons["text_output_tokens"]["difference"] is None
            assert comparisons["text_cached_input_tokens"]["unknownRelayUsageRequests"] == 1
            assert comparisons["image_output_count_le_2_61m"]["difference"] is None
            assert comparisons["image_output_count_4_63m_to_8_30m"]["unknownRelayUsageRequests"] == 2
            assert comparisons["image_input_count"]["difference"] == 0
            assert {(item["requestId"], item["reason"]) for item in incomplete["unmatchedRelayCalls"]} == {
                ("chatcmpl-legacy", "missing_settlement_time"), ("chatcmpl-missing-cache", "missing_usage"),
                ("img-no-sizes", "missing_usage"), ("img-outside-bands", "missing_usage"),
            }
            assert [(item["billDetailId"], item["reason"]) for item in incomplete["unmatchedProviderLines"]] == [
                ("wrong-unit", "unsupported_usage_unit"),
            ]


def test_bill_attribution_requires_exact_evidence_for_the_active_revision(tmp_path: Path) -> None:
    with _client(tmp_path / "bill-attribution.sqlite") as client:
        alice, bob = _customer(client), _customer(client, "bob")
        with client.app.state.db.connect() as connection:
            _price_customer(connection, alice)
            _task(connection, alice, "vid_new", tokens=45000)
            _task(connection, alice, "vid_pending", tokens=None)
            statements.confirm_statement(connection, alice.project_id, MONTH)
            _call(connection, alice, "chatcmpl-a", provider_id="official-chat-a")
            _call(connection, alice, "chatcmpl-b", provider_id="shared-id")
            _call(connection, bob, "chatcmpl-c", provider_id="shared-id")
            _call(connection, bob, "chatcmpl-prior", provider_id="prior-month", settled_at=START - 1)
            _call(connection, bob, "img_b", model=IMAGE_MODEL)
            lines = (
                _line("video"), _line("unpriced-video"), _line("chat", ConfigName=TEXT_UPSTREAM),
                _line("image", ConfigName=IMAGE_UPSTREAM),
                _line("explicit-project"), _line("wrong-model"), _line("not-unique", ConfigName=TEXT_UPSTREAM),
                _line("other-month", BusiPeriod="2025-01"), _line("wrong-time", ConfigName=TEXT_UPSTREAM),
                _line("no-correlation", InstanceNo="provider-vid_new", ResourceID="official-chat-a"),
            )
            _import(connection, *lines)
            before = _report(connection)
            assert "attribution" not in before
            mappings = {
                "video": {"providerTaskId": "provider-vid_new"},
                "unpriced-video": {"localRequestId": "vid_pending"},
                "chat": {"providerRequestId": "official-chat-a"},
                "image": {"localRequestId": "img_b"},
                "explicit-project": {"projectId": bob.project_id},
                "wrong-model": {"localRequestId": "img_b"},
                "not-unique": {"providerRequestId": "shared-id"},
                "other-month": {"projectId": alice.project_id},
                "wrong-time": {"providerRequestId": "prior-month"},
            }
            loaded = {"scope": before["source"], "lines": mappings}
            with pytest.raises(ValueError, match="explicit confirmation"):
                _report(connection, correlation_map=loaded)
            options = {"confirm_usage_scope": True}
            with pytest.raises(ValueError, match="exactly match"):
                _report(connection, **options, correlation_map={**loaded, "scope": {**before["source"], "ownerId": "0"}})
            with pytest.raises(ValueError, match="outside the active import"):
                _report(connection, **options, correlation_map={**loaded, "lines": {"wrong-id": mappings["video"]}})
            with pytest.raises(ValueError, match="exactly one"):
                _report(connection, **options, correlation_map={**loaded, "lines": {
                    "video": {"providerTaskId": "provider-vid_new", "projectId": alice.project_id},
                }})

            report = _report(connection, **options, correlation_map=loaded)
            assert report["provider"] == before["provider"] and report["customers"] == before["customers"]
            attribution = report["attribution"]
            assigned = {item["billDetailId"]: (item["projectId"], item["localRequestId"])
                        for item in attribution["attributedProviderLines"]}
            assert assigned == {
                "video": (alice.project_id, "vid_new"), "chat": (alice.project_id, "chatcmpl-a"),
                "image": (bob.project_id, "img_b"), "explicit-project": (bob.project_id, None),
                "unpriced-video": (alice.project_id, "vid_pending"),
            }
            assert {item["billDetailId"]: item["reason"] for item in attribution["unmatchedProviderLines"]} == {
                "wrong-model": "mapped_request_model_mismatch", "not-unique": "mapped_request_ambiguous",
                "other-month": "different_business_period", "no-correlation": "no_verified_attribution",
                "wrong-time": "mapped_request_not_found_in_scope",
            }
            by_project = {item["projectId"]: item for item in attribution["projects"]}
            assert by_project[alice.project_id]["attributedProvider"]["payableAmount"] == "27.15"
            assert by_project[alice.project_id]["customers"]["confirmedPayableAmount"] == "0.05"
            assert attribution["unattributedProvider"]["lineCount"] == 5
            by_request = {item["localRequestId"]: item for item in attribution["requests"]}
            assert by_request["chatcmpl-a"]["modality"] == "text"
            assert by_request["chatcmpl-a"]["customer"] == {"currency": "CNY", "status": "unbilled", "amount": None}
            assert by_request["img_b"]["attributedProvider"]["lineCount"] == 1
            assert by_request["vid_new"]["customer"] == {"currency": "CNY", "status": "priced", "amount": "0.045000"}
            assert by_request["vid_pending"]["customer"] == {"currency": "CNY", "status": "pending", "amount": None}

            _import(connection, *lines, _line("revision"), replace=True)
            with pytest.raises(ValueError, match="exactly match"):
                _report(connection, **options, correlation_map=loaded)


def test_customer_receivables_are_independent_and_provider_costs_are_not_exposed(tmp_path: Path) -> None:
    with _client(tmp_path / "isolated.sqlite") as client:
        alice, bob = _customer(client), _customer(client, "bob")
        supplier_id = "synthetic-provider-detail-private"
        with client.app.state.db.connect() as connection:
            _price_customer(connection, alice)
            _price_customer(connection, bob)
            _task(connection, alice, "vid_alice", tokens=45000)
            _task(connection, bob, "vid_bob", tokens=120000)
            statements.confirm_statement(connection, alice.project_id, MONTH)
        path = f"/api/customer/billing?month={MONTH}"
        before = client.get(path, headers=alice.session)
        assert before.status_code == 200 and before.json()["summary"]["payableAmount"] == "0.05"

        with client.app.state.db.connect() as connection:
            _import(connection, _line(supplier_id))
            report = _report(connection)
            assert report["provider"]["cashPaidAmount"] == "8.947"
            assert report["customers"] == {"currency": "CNY", "statementCount": 1, "confirmedPayableAmount": "0.05"}
        after = client.get(path, headers=alice.session)
        assert after.status_code == 200 and after.json() == before.json()
        assert supplier_id not in after.text and PAYER not in after.text and "cashPaidAmount" not in after.text
        assert client.get("/api/customer/provider-costs", headers=alice.session).status_code == 404
        foreign = client.get(f"/api/customer/billing/{before.json()['id']}", headers=bob.session)
        assert foreign.status_code == 404 and foreign.json()["detail"]["code"] == "statement_not_found"
        with client.app.state.db.connect() as connection:
            statements.confirm_statement(connection, bob.project_id, MONTH)
            both_confirmed = _report(connection)
            assert both_confirmed["customers"] == {"currency": "CNY", "statementCount": 2, "confirmedPayableAmount": "0.17"}
            assert both_confirmed["provider"] == report["provider"]


def test_operator_import_and_report_cli_use_the_initialized_local_database(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite"
    with _client(path):
        pass
    source = tmp_path / "synthetic-bill.json"
    source.write_text(json.dumps(_envelope(_line()), ensure_ascii=False), encoding="utf-8")
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH", "TMP", "TEMP") if name in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"

    def command(action: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-m", "app.billing_cli", "--database", str(path), action,
             "--payer-id", PAYER, "--owner-id", OWNER, "--product", PRODUCT, "--month", MONTH, *args],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    first = command("import-provider-bill", "--file", str(source))
    repeat = command("import-provider-bill", "--file", str(source))
    report = command("provider-cost-report")
    assert first.returncode == repeat.returncode == report.returncode == 0, first.stderr + repeat.stderr + report.stderr
    loaded, replayed, costs = map(json.loads, (first.stdout, repeat.stdout, report.stdout))
    assert loaded == {"importId": costs["source"]["importId"], "rows": 1, "disposition": "imported"}
    assert replayed == {**loaded, "disposition": "unchanged"}
    assert costs["provider"]["payableAmount"] == "9.05" and costs["customers"]["confirmedPayableAmount"] == "0.00"
    assert costs["usage"]["scopeConfirmed"] is False

    correlation = tmp_path / "correlation.json"
    correlation.write_text(json.dumps({"scope": costs["source"], "lines": {}}), encoding="utf-8")
    request = command("provider-cost-report", "--confirm-usage-scope", "--correlation-map", str(correlation))
    assert request.returncode == 0, request.stderr
    assert json.loads(request.stdout)["attribution"]["unattributedProvider"]["lineCount"] == 1
    correlation.write_text('{"scope":{},"lines":{"same":{},"same":{}}}', encoding="utf-8")
    duplicate = command("provider-cost-report", "--confirm-usage-scope", "--correlation-map", str(correlation))
    assert duplicate.returncode == 1 and "without duplicate fields" in duplicate.stderr

    evidence = tmp_path / "request-evidence.json"
    evidence_scope = {field: costs["source"][field] for field in ("payerId", "ownerId", "product", "month")}
    evidence.write_text(json.dumps({"scope": evidence_scope}), encoding="utf-8")
    ignored = command("provider-cost-report", "--request-evidence", str(evidence))
    assert ignored.returncode == 1 and "requires explicit confirmation" in ignored.stderr
    scoped = command("provider-cost-report", "--confirm-usage-scope", "--request-evidence", str(evidence))
    assert scoped.returncode == 0, scoped.stderr
    assert json.loads(scoped.stdout)["requestUsage"]["matched"] == []
