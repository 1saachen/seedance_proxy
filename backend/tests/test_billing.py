import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import httpx
from fastapi.testclient import TestClient

from app import billing
from app.accounts import initialize as initialize_accounts
from app.config import Settings
from app.main import create_app
from app.model_api import TASKS_PATH


MODEL = "doubao-seedance-2.5"
UPSTREAM_MODEL = "doubao-seedance-2-5-260628"
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
CHANGE = START + timedelta(days=1)
TEXT = {"type": "text", "text": "Clouds crossing a mountain ridge"}
VIDEO = {"type": "video_url", "video_url": {"url": "https://media.example.com/source.mp4"}}


@dataclass(frozen=True)
class Customer:
    project_id: str
    key_id: str
    auth: dict[str, str]
    usage_auth: dict[str, str]


class MockArk:
    def __init__(self) -> None:
        self.submissions: list[dict] = []
        self.results: dict[str, dict] = {}
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == TASKS_PATH
            self.submissions.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"provider-task-{len(self.submissions)}"})
        assert request.method == "GET"
        provider_id = request.url.path.removeprefix(TASKS_PATH + "/")
        self.queries.append(provider_id)
        return httpx.Response(200, json={"id": provider_id, **self.results[provider_id]})


def _client(path: Path, mock: httpx.MockTransport) -> TestClient:
    return TestClient(create_app(Settings(
        database_path=path, registration_enabled=True, cookie_secure=False,
        ark_api_key="test-only-inference-key", ark_asset_access_key=None, ark_asset_secret_key=None,
        video_daily_limit=100, video_poll_seconds=0,
        upload_directory=None, upload_public_origin="", upload_cleanup_seconds=0,
    ), ark_transport=mock))


def _customer(client: TestClient, name: str = "alice") -> Customer:
    client.cookies.clear()
    registered = client.post("/api/customer/register", json={"email": f"{name}@example.com", "password": "Example-only-pass-42"})
    assert registered.status_code == 201, registered.text
    account = registered.json()
    created = client.post(
        "/api/customer/keys", headers={"X-CSRF-Token": account["csrfToken"]}, json={"name": "billing test"},
    )
    assert created.status_code == 201
    key = created.json()
    return Customer(
        account["customer"]["projectId"], key["key"]["id"], {"Authorization": f"Bearer {key['secret']}"},
        {"Cookie": f"seedance_customer_session={client.cookies.get('seedance_customer_session')}"},
    )


def _create(
    client: TestClient, customer: Customer, *, video: bool = False, resolution: str = "720p",
    created_at: datetime = CHANGE, idempotency_key: str | None = None,
) -> str:
    body = {"model": MODEL, "resolution": resolution, "content": [TEXT, VIDEO] if video else [TEXT]}
    headers = customer.auth | ({"Idempotency-Key": idempotency_key} if idempotency_key else {})
    result = client.post(TASKS_PATH, headers=headers, json=body)
    assert result.status_code == 200, result.text
    task_id = result.json()["id"]
    with client.app.state.db.connect() as connection:
        connection.execute(
            "UPDATE video_tasks SET created_at = ? WHERE id = ?", (created_at.strftime("%Y-%m-%d %H:%M:%S"), task_id),
        )
    return task_id


def _refresh(client: TestClient, customer: Customer, task_id: str) -> dict:
    response = client.get(f"{TASKS_PATH}/{task_id}", headers=customer.auth)
    assert response.status_code == 200, response.text
    return response.json()


def _charge(client: TestClient, task_id: str) -> dict:
    with client.app.state.db.connect() as connection:
        row = connection.execute("SELECT * FROM video_charges WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return dict(row)


def _price(connection, micros: int, *, since: datetime = START, resolution: str = "720p", video: bool = False) -> int:
    return billing.add_price(
        connection, upstream_model=UPSTREAM_MODEL, resolution=resolution, contains_video_input=video,
        effective_at=int(since.timestamp()), unit_price_micros_per_million=micros, source="Synthetic RP-201 price",
    )


def _terms(connection, customer: Customer, payable_bps: int, *, since: datetime = START, enabled: bool = True) -> int:
    return billing.add_project_terms(
        connection, project_id=customer.project_id, effective_at=int(since.timestamp()), enabled=enabled,
        payable_bps=payable_bps,
    )


def test_charge_snapshots_task_time_price_dimensions_discount_and_customer_scope(tmp_path: Path) -> None:
    ark = MockArk()
    with _client(tmp_path / "priced.sqlite", httpx.MockTransport(ark)) as client:
        alice, bob = _customer(client), _customer(client, "bob")
        with client.app.state.db.connect() as connection:
            old_price = _price(connection, 3_000_000)
            new_price = _price(connection, 4_000_000, since=CHANGE)
            video_price = _price(connection, 7_000_000, video=True, since=CHANGE)
            _price(connection, 99_000_000, resolution="480p")
            old_terms = _terms(connection, alice, 5000)
            new_terms = _terms(connection, alice, 7500, since=CHANGE)
        before = _create(client, alice, created_at=CHANGE - timedelta(seconds=1), resolution="480p")
        boundary = _create(client, alice)
        video = _create(client, alice, video=True)
        foreign = _create(client, bob)
        for task_number, (task_id, owner) in enumerate(((before, alice), (boundary, alice), (video, alice), (foreign, bob)), 1):
            ark.results[f"provider-task-{task_number}"] = {
                "status": "succeeded", "resolution": "720p", "usage": {"completion_tokens": 1, "total_tokens": 17},
            }
            assert _refresh(client, owner, task_id)["status"] == "succeeded"

        for task_id, price, terms, payable, unit, listed, amount, has_video in (
            (before, old_price, old_terms, 5000, 3_000_000, 3, 2, False),
            (boundary, new_price, new_terms, 7500, 4_000_000, 4, 3, False),
            (video, video_price, new_terms, 7500, 7_000_000, 7, 5, True),
        ):
            row = _charge(client, task_id)
            assert (row["project_id"], row["key_id"], row["model"], row["upstream_model"]) == (
                alice.project_id, alice.key_id, MODEL, UPSTREAM_MODEL,
            )
            assert (row["status"], row["resolution"], row["contains_video_input"], row["output_tokens"]) == (
                "priced", "720p", int(has_video), 1,
            )
            assert (row["price_id"], row["terms_id"], row["payable_bps"], row["unit_price_micros_per_million"]) == (
                price, terms, payable, unit,
            )
            assert (row["list_amount_micros"], row["amount_micros"]) == (listed, amount)

        response = client.get("/api/customer/usage?month=2025-01", headers=alice.usage_auth)
        assert response.status_code == 200
        usage = response.json()
        assert usage["summary"] == {"calls": 3, "successful": 3, "outputTokens": 3, "pricedCalls": 3}
        by_id = {item["id"]: item for item in usage["items"]}
        assert set(by_id) == {before, boundary, video}
        assert by_id[before]["billing"] == {
            "status": "priced", "pendingReason": None, "resolution": "720p", "containsVideoInput": False,
            "priceVersion": old_price, "termsVersion": old_terms, "unitPrice": "3.000000", "payableBps": 5000,
            "listAmount": "0.000003", "amount": "0.000002",
        }
        foreign_usage = client.get("/api/customer/usage?month=2025-01", headers=bob.usage_auth).json()
        assert [item["id"] for item in foreign_usage["items"]] == [foreign]
        assert foreign_usage["summary"]["pricedCalls"] == 0 and _charge(client, foreign)["status"] == "unbilled"


def test_disabled_terms_do_not_charge_success_and_other_terminal_states_have_no_charge(tmp_path: Path) -> None:
    ark = MockArk()
    with _client(tmp_path / "disabled.sqlite", httpx.MockTransport(ark)) as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _price(connection, 8_000_000)
            _terms(connection, customer, 10000)
            disabled = _terms(connection, customer, 10000, since=CHANGE, enabled=False)
        for idx, status in enumerate(("succeeded", "failed", "cancelled", "expired", "running"), 1):
            task = _create(client, customer)
            ark.results[f"provider-task-{idx}"] = {
                "status": status, "resolution": "720p", "usage": {"completion_tokens": 100},
            }
            _refresh(client, customer, task)
            if status == "succeeded":
                row = _charge(client, task)
                assert row["status"] == "unbilled" and row["terms_id"] == disabled
                assert row["output_tokens"] == 100 and row["amount_micros"] is None
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_charges").fetchone()[0] == 1


def test_missing_inputs_enter_pending_and_explicit_zero_is_priced(tmp_path: Path) -> None:
    ark = MockArk()
    with _client(tmp_path / "pending.sqlite", httpx.MockTransport(ark)) as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _terms(connection, customer, 10000)
            _price(connection, 3_000_000)
            _price(connection, 0, resolution="480p")
        cases = [
            ({"resolution": "720p", "usage": {"total_tokens": 125}}, "pending", "missing_usage", None, None),
            ({"usage": {"completion_tokens": 121}}, "pending", "missing_pricing_dimensions", 121, None),
            ({"resolution": "1080p", "usage": {"completion_tokens": 120}}, "pending", "missing_price", 120, None),
            ({"resolution": "720p", "usage": {"completion_tokens": 0}}, "priced", None, 0, 0),
            ({"resolution": "480p", "usage": {"completion_tokens": 119}}, "priced", None, 119, 0),
        ]
        for idx, (details, status, reason, tokens, amount) in enumerate(cases, 1):
            task = _create(client, customer)
            ark.results[f"provider-task-{idx}"] = {"status": "succeeded", **details}
            _refresh(client, customer, task)
            row = _charge(client, task)
            assert (row["status"], row["pending_reason"], row["output_tokens"], row["amount_micros"]) == (
                status, reason, tokens, amount,
            )


def test_reconciliation_prices_pending_after_rate_added_and_is_idempotent(tmp_path: Path) -> None:
    ark = MockArk()
    with _client(tmp_path / "reconciled.sqlite", httpx.MockTransport(ark)) as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _terms(connection, customer, 8000)
        task = _create(client, customer)
        ark.results["provider-task-1"] = {"status": "succeeded", "resolution": "720p", "usage": {"completion_tokens": 1}}
        _refresh(client, customer, task)
        assert _charge(client, task)["pending_reason"] == "missing_price"
        with client.app.state.db.connect() as connection:
            price = _price(connection, 3_125_000)
            assert billing.reconcile_batch(connection, include_pending=False)[1] == 0
            assert billing.reconcile_batch(connection, include_pending=True)[1] == 1
        priced = _charge(client, task)
        assert (priced["status"], priced["price_id"], priced["list_amount_micros"], priced["amount_micros"]) == ("priced", price, 3, 3)
        with client.app.state.db.connect() as connection:
            _price(connection, 999_000_000, since=CHANGE)
            _terms(connection, customer, 5000, since=CHANGE)
            assert billing.reconcile_batch(connection, include_pending=True)[1] == 0
        assert _charge(client, task) == priced


def test_concurrent_success_observations_record_one_charge(tmp_path: Path) -> None:
    ark = MockArk()
    both_polling = asyncio.Event()

    async def concurrent_upstream(request: httpx.Request) -> httpx.Response:
        response = ark(request)
        if request.method == "GET":
            if len(ark.queries) == 2:
                both_polling.set()
            await asyncio.wait_for(both_polling.wait(), timeout=5)
        return response

    with _client(tmp_path / "concurrent.sqlite", httpx.MockTransport(concurrent_upstream)) as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _terms(connection, customer, 9000)
            _price(connection, 10_000_000)
        task = _create(client, customer, idempotency_key="concurrent-charge")
        ark.results["provider-task-1"] = {"status": "succeeded", "resolution": "720p", "usage": {"completion_tokens": 10}}
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(_refresh, client, customer, task)
            second = workers.submit(_refresh, client, customer, task)
            assert first.result(timeout=10) == second.result(timeout=10)
        assert _create(client, customer, idempotency_key="concurrent-charge") == task
        assert _refresh(client, customer, task)["usage"]["completion_tokens"] == 10
        assert ark.queries == ["provider-task-1", "provider-task-1"] and len(ark.submissions) == 1
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_charges WHERE task_id = ?", (task,)).fetchone()[0] == 1
        assert _charge(client, task)["amount_micros"] == 90


def test_charge_failure_preserves_success_and_restarts_with_a_single_reconciled_charge(tmp_path: Path) -> None:
    ark = MockArk()
    path = tmp_path / "restart.sqlite"
    with _client(path, httpx.MockTransport(ark)) as client:
        customer = _customer(client)
        with client.app.state.db.connect() as connection:
            _terms(connection, customer, 10000)
            _price(connection, 2_000_000)
            connection.execute("""
                CREATE TRIGGER simulated_charge_failure BEFORE INSERT ON video_charges
                BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END
            """)
        task = _create(client, customer)
        ark.results["provider-task-1"] = {"status": "succeeded", "resolution": "720p", "usage": {"completion_tokens": 42}}
        terminal = _refresh(client, customer, task)
        assert terminal["status"] == "succeeded" and terminal["usage"]["completion_tokens"] == 42
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_charges").fetchone()[0] == 0
            connection.execute("DROP TRIGGER simulated_charge_failure")
    with _client(path, httpx.MockTransport(ark)) as client:
        assert _refresh(client, customer, task) == terminal
        assert _charge(client, task)["amount_micros"] == 84
        assert ark.queries == ["provider-task-1"]
        with client.app.state.db.connect() as connection:
            assert billing.reconcile_batch(connection, include_pending=True)[1] == 0


def test_repeated_migration_preserves_legacy_tasks_and_does_not_charge_existing_projects(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        initialize_accounts(connection)
        connection.executescript("""
            CREATE TABLE video_tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                key_id TEXT NOT NULL REFERENCES business_keys(id),
                idempotency_key TEXT,
                request_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                upstream_id TEXT UNIQUE,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                requested_resolution TEXT,
                contains_video_input INTEGER NOT NULL CHECK (contains_video_input IN (0, 1)),
                output_tokens INTEGER CHECK (output_tokens >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TEXT,
                last_polled_at TEXT,
                UNIQUE(key_id, idempotency_key)
            );
            INSERT INTO projects VALUES ('old-project', '2025-01-01 00:00:00');
            INSERT INTO business_keys VALUES ('old-key', 'old-project', 'Legacy', 'test', 'synthetic-key-hash', '2025-01-01 00:00:00', NULL);
            INSERT INTO video_tasks (
                id, project_id, key_id, request_hash, model, upstream_id, status, result_json, requested_resolution,
                contains_video_input, output_tokens, created_at, settled_at
            ) VALUES (
                'vid_legacy', 'old-project', 'old-key', 'synthetic', 'doubao-seedance-2.5', 'legacy-provider-id', 'succeeded',
                '{"resolution":"720p"}', '720p', 0, 123, '2025-01-02 00:00:00', '2025-01-02 00:01:00'
            );
        """)
    ark = MockArk()
    with _client(path, httpx.MockTransport(ark)) as client:
        initial_charge = _charge(client, "vid_legacy")
        assert initial_charge["status"] == "unbilled" and initial_charge["amount_micros"] is None
        assert initial_charge["output_tokens"] == 123 and initial_charge["upstream_model"] is None
    with _client(path, httpx.MockTransport(ark)) as client:
        with client.app.state.db.connect() as connection:
            row = connection.execute("SELECT * FROM video_tasks WHERE id = 'vid_legacy'").fetchone()
            assert (row["project_id"], row["key_id"], row["upstream_id"], row["output_tokens"], row["upstream_model"]) == (
                "old-project", "old-key", "legacy-provider-id", 123, None,
            )
            assert connection.execute("SELECT COUNT(*) FROM video_charges").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _charge(client, "vid_legacy") == initial_charge
        assert not ark.submissions and not ark.queries
