from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

from app import billing, statements
from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.main import create_app


MODEL = "doubao-seedance-2.5"
UPSTREAM_MODEL = "doubao-seedance-2-5-260628"
CREATED_AT = "2024-12-01 00:00:00"
JANUARY = "2025-01"
FEBRUARY = "2025-02"
MARCH = "2025-03"
APRIL = "2025-04"
MAY = "2025-05"
SHANGHAI = timezone(timedelta(hours=8))
BILLING = "/api/customer/billing"


@dataclass(frozen=True)
class Customer:
    project_id: str
    key_id: str
    session: dict[str, str]
    bearer: dict[str, str]


def _client(path: Path) -> TestClient:
    def no_provider_calls(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Statement tests must not call upstream ({request.method})")

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
        "/api/customer/keys", headers={"X-CSRF-Token": account["csrfToken"]}, json={"name": "statement test"},
    )
    assert created.status_code == 201
    key = created.json()
    session = {"Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}"}
    client.cookies.clear()
    return Customer(
        project_id=account["customer"]["projectId"], key_id=key["key"]["id"], session=session,
        bearer={"Authorization": f"Bearer {key['secret']}"},
    )


def _price_customer(connection: sqlite3.Connection, customer: Customer) -> None:
    # One micro-yuan per token makes hand-calculated statement totals easy to verify.
    billing.add_price(
        connection, upstream_model=UPSTREAM_MODEL, resolution="720p", contains_video_input=False,
        effective_at=0, unit_price_micros_per_million=1_000_000, source="Synthetic RP-202 price",
    )
    billing.add_project_terms(connection, project_id=customer.project_id, effective_at=0, enabled=True, payable_bps=10000)


def _charge(
    connection: sqlite3.Connection, customer: Customer, task_id: str, *,
    settled_at: str, micros: int | None, created_at: str = CREATED_AT, model: str = MODEL,
) -> None:
    row = connection.execute("""
        INSERT INTO video_tasks (
            id, project_id, key_id, request_hash, model, upstream_model, status, result_json,
            requested_resolution, contains_video_input, output_tokens, created_at, settled_at
        ) VALUES (?, ?, ?, 'synthetic', ?, ?, 'succeeded', '{"resolution":"720p"}', '720p', 0, ?, ?, ?)
        RETURNING *
    """, (task_id, customer.project_id, customer.key_id, model, UPSTREAM_MODEL, micros, created_at, settled_at)).fetchone()
    billing.record_task(connection, row)


def _preview(client: TestClient, customer: Customer, month: str, **extra: int) -> dict:
    result = client.get(BILLING, headers=customer.session, params={"month": month, **extra})
    assert result.status_code == 200, result.text
    assert result.headers["Cache-Control"] == "no-store"
    return result.json()


def _confirm(client: TestClient, customer: Customer, month: str, *, now: datetime | None = None) -> dict:
    with client.app.state.db.connect() as connection:
        return statements.confirm_statement(connection, customer.project_id, month, now=now)


def _error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code


def test_statement_uses_beijing_settlement_boundaries_and_rounds_aggregate_once(tmp_path: Path) -> None:
    with _client(tmp_path / "boundaries.sqlite") as client:
        owner = _customer(client)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, owner)
            _charge(connection, owner, "vid_previous", micros=8000, settled_at="2024-12-31 15:59:59")
            _charge(connection, owner, "vid_first", micros=2499, settled_at="2024-12-31 16:00:00")
            _charge(connection, owner, "vid_last", micros=2501, settled_at="2025-01-31 15:59:59")
            _charge(connection, owner, "vid_next", micros=7000, settled_at="2025-01-31 16:00:00")
            _charge(connection, owner, "vid_pending", micros=None, settled_at="2025-01-15 12:00:00")

        preview = _preview(client, owner, JANUARY)
        assert (preview["id"], preview["month"], preview["currency"], preview["status"], preview["confirmedAt"]) == (
            None, JANUARY, "CNY", "estimate", None,
        )
        assert preview["summary"] == {
            "usageCalls": 2, "lateCalls": 0, "pendingCalls": 1,
            "listAmount": "0.005000", "amount": "0.005000", "payableAmount": "0.01",
        }
        items = {row["taskId"]: row for row in preview["items"]}
        assert set(items) == {"vid_first", "vid_last"}
        assert (items["vid_first"]["sourceMonth"], items["vid_first"]["kind"], items["vid_first"]["amount"]) == (
            JANUARY, "usage", "0.002499",
        )
        first_page = _preview(client, owner, JANUARY, page=1, pageSize=1)
        second_page = _preview(client, owner, JANUARY, page=2, pageSize=1)
        assert first_page["pagination"] == {"page": 1, "pageSize": 1, "total": 2}
        assert second_page["pagination"] == {"page": 2, "pageSize": 1, "total": 2}
        assert [*first_page["items"], *second_page["items"]] == preview["items"]
        assert [row["taskId"] for row in _preview(client, owner, "2024-12")["items"]] == ["vid_previous"]
        assert [row["taskId"] for row in _preview(client, owner, FEBRUARY)["items"]] == ["vid_next"]


def test_current_month_is_estimate_and_only_closed_months_can_be_confirmed(tmp_path: Path) -> None:
    with _client(tmp_path / "open.sqlite") as client:
        owner = _customer(client)
        current = datetime.now(SHANGHAI).strftime("%Y-%m")
        default = client.get(BILLING, headers=owner.session)
        assert default.status_code == 200
        assert (default.json()["month"], default.json()["status"]) == (current, "estimate")
        _error(client.get(BILLING, params={"month": "2090-01"}, headers=owner.session), 422, "future_month")

        # It is February in Beijing even though it is still January in UTC.
        midnight = datetime(2025, 1, 31, 16, tzinfo=UTC)
        with pytest.raises(statements.StatementError) as error:
            _confirm(client, owner, FEBRUARY, now=midnight)
        assert error.value.code == "cannot_confirm_open_month"
        with pytest.raises(statements.StatementError):
            _confirm(client, owner, MARCH, now=midnight)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, owner)
            _charge(connection, owner, "vid_closed", micros=5000, settled_at="2025-01-31 15:59:59")
        assert _confirm(client, owner, JANUARY, now=midnight)["status"] == "confirmed"


def test_frozen_lines_remain_stable_and_late_prices_go_to_first_open_month(tmp_path: Path) -> None:
    with _client(tmp_path / "late.sqlite") as client:
        owner = _customer(client)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, owner)
            _charge(connection, owner, "vid_jan", micros=225000, settled_at="2025-01-20 00:00:00")
            _charge(connection, owner, "vid_feb", micros=130000, settled_at="2025-02-20 00:00:00")
            _charge(connection, owner, "vid_pending", micros=None, settled_at="2025-01-29 00:00:00")
            _charge(connection, owner, "vid_apr", micros=40000, settled_at="2025-04-05 00:00:00")

        january = _confirm(client, owner, JANUARY)
        february = _confirm(client, owner, FEBRUARY)
        april = _confirm(client, owner, APRIL)
        january_path = f"{BILLING}/{january['id']}"
        before = client.get(january_path, headers=owner.session).json()
        before_csv = client.get(f"{january_path}/export.csv", headers=owner.session).content
        assert before == january and before["status"] == "confirmed"
        assert before["summary"]["amount"] == "0.225000"
        assert [row["taskId"] for row in before["items"]] == ["vid_jan"]

        with client.app.state.db.connect() as connection:
            _charge(connection, owner, "vid_late", micros=110000, settled_at="2025-01-30 00:00:00")
            connection.execute("UPDATE video_tasks SET output_tokens = 220000 WHERE id = 'vid_pending'")
            billing.reconcile_batch(connection, include_pending=True)
            # Mutate the live source fixture to prove the confirmed view reads stored snapshots.
            connection.execute("UPDATE video_charges SET amount_micros = 1 WHERE task_id = 'vid_jan'")

        assert _confirm(client, owner, JANUARY) == january
        assert _preview(client, owner, JANUARY) == january
        assert _preview(client, owner, FEBRUARY) == february
        assert _preview(client, owner, APRIL) == april
        assert client.get(january_path, headers=owner.session).json() == before
        assert client.get(f"{january_path}/export.csv", headers=owner.session).content == before_csv
        march = _preview(client, owner, MARCH)
        assert march["summary"] == {
            "usageCalls": 0, "lateCalls": 2, "pendingCalls": 0,
            "listAmount": "0.330000", "amount": "0.330000", "payableAmount": "0.33",
        }
        assert {(row["taskId"], row["sourceMonth"], row["kind"]) for row in march["items"]} == {
            ("vid_late", JANUARY, "late_usage"), ("vid_pending", JANUARY, "late_usage"),
        }
        assert _confirm(client, owner, MARCH)["items"] == march["items"]
        assert _preview(client, owner, MAY)["items"] == []


def test_statement_detail_and_csv_are_session_scoped_and_escape_formulas(tmp_path: Path) -> None:
    with _client(tmp_path / "scoped.sqlite") as client:
        alice, bob = _customer(client), _customer(client, "bob")
        models = ("=测试样本", "+test", "-test", "@test")
        with client.app.state.db.connect() as connection:
            _price_customer(connection, alice)
            _price_customer(connection, bob)
            for number, model in enumerate(models):
                _charge(connection, alice, f"vid_csv_{number}", model=model, micros=10000, settled_at="2025-01-15 00:00:00")
            _charge(connection, bob, "vid_bob", micros=20000, settled_at="2025-01-15 00:00:00")
        statement = _confirm(client, alice, JANUARY)
        detail = f"{BILLING}/{statement['id']}"
        export = f"{detail}/export.csv"
        for path in (BILLING, detail, export):
            _error(client.get(path), 401, "customer_login_required")
            _error(client.get(path, headers=alice.bearer), 401, "customer_login_required")
        for path in (detail, export):
            _error(client.get(path, headers=bob.session), 404, "statement_not_found")
        _error(client.get(f"{BILLING}/stmt_{'0' * 32}", headers=alice.session), 404, "statement_not_found")
        own_bob = _preview(client, bob, JANUARY)
        assert own_bob["status"] == "estimate" and [row["taskId"] for row in own_bob["items"]] == ["vid_bob"]
        page = client.get(detail, headers=alice.session, params={"page": 2, "pageSize": 2})
        assert page.status_code == 200
        assert page.json()["pagination"] == {"page": 2, "pageSize": 2, "total": 4}
        assert len(page.json()["items"]) == 2

        exported = client.get(export, headers=alice.session)
        assert exported.status_code == 200
        assert exported.headers["Content-Type"].startswith("text/csv")
        assert exported.headers["Content-Disposition"].startswith("attachment;")
        assert exported.headers["Cache-Control"] == "no-store"
        assert exported.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert {row["statementId"] for row in rows} == {statement["id"]}
        assert {row["month"] for row in rows} == {JANUARY}
        assert {row["model"] for row in rows} == {f"'{model}" for model in models}
        assert {row["taskId"] for row in rows} == {f"vid_csv_{number}" for number in range(4)}


def test_concurrent_confirmation_creates_one_statement_and_one_snapshot(tmp_path: Path) -> None:
    with _client(tmp_path / "concurrent.sqlite") as client:
        owner = _customer(client)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, owner)
            _charge(connection, owner, "vid_race", micros=5000, settled_at="2025-01-15 00:00:00")

        race = Barrier(2)

        def confirm() -> dict:
            with client.app.state.db.connect() as connection:
                race.wait(timeout=5)
                return statements.confirm_statement(connection, owner.project_id, JANUARY)

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(confirm)
            second = workers.submit(confirm)
            frozen = first.result(timeout=10)
            assert second.result(timeout=10) == frozen
        assert frozen["status"] == "confirmed" and frozen["id"].startswith("stmt_")
        assert [row["taskId"] for row in frozen["items"]] == ["vid_race"]
        assert frozen["summary"]["payableAmount"] == "0.01"
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM billing_statements").fetchone()[0] == 1
            assert connection.execute("SELECT payable_fen FROM billing_statements").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM billing_statement_lines").fetchone()[0] == 1


def test_operator_cli_replays_frozen_result_and_rejects_current_month(tmp_path: Path) -> None:
    database = tmp_path / "cli.sqlite"
    with _client(database) as client:
        owner = _customer(client)
        with client.app.state.db.connect() as connection:
            _price_customer(connection, owner)
            _charge(connection, owner, "vid_cli", micros=45000, settled_at="2025-01-15 00:00:00")

    environment = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH", "TMP", "TEMP") if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"

    def confirm(month: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", "-m", "app.billing_cli", "--database", str(database), "confirm-statement",
             "--project-id", owner.project_id, "--month", month],
            cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    first, replay = confirm(JANUARY), confirm(JANUARY)
    assert first.returncode == replay.returncode == 0, first.stderr + replay.stderr
    result = json.loads(first.stdout)
    assert result == {
        "statementId": result["statementId"], "month": JANUARY, "status": "confirmed", "payableAmount": "0.05",
    }
    assert result["statementId"].startswith("stmt_") and json.loads(replay.stdout) == result
    rejected = confirm(datetime.now(SHANGHAI).strftime("%Y-%m"))
    assert rejected.returncode == 1 and rejected.stdout == ""
    assert "cannot_confirm_open_month" in rejected.stderr
