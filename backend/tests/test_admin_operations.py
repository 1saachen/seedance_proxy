from datetime import datetime
from pathlib import Path
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import admin_auth, billing, key_limits, statements
from app.config import Settings
from app.main import create_app
from app.model_api import DRAFT_MODEL, SEEDANCE_MODELS


ADMIN = "/api/admin"
CUSTOMER = "/api/customer"
ADMIN_PASSWORD = "synthetic-admin-passphrase"
CUSTOMER_PASSWORD = "synthetic-customer-passphrase"
UPSTREAM_MODEL = SEEDANCE_MODELS[DRAFT_MODEL].upstream_id
MONTH = "2025-01"
EMPTY_KEY_USAGE = {
    "activeTasks": 0, "activeTextRequests": 0, "activeImageRequests": 0, "activeConcurrentRequests": 0,
    "requestsLastMinute": 0,
}


def _client(path: Path) -> TestClient:
    def no_provider_calls(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Admin operations must not call the provider ({request.method})")

    transport = httpx.MockTransport(no_provider_calls)
    return TestClient(create_app(Settings(
        database_path=path, cookie_secure=False, registration_enabled=False,
        ark_api_key=None, ark_asset_access_key=None, ark_asset_secret_key=None,
        video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
    ), ark_transport=transport, ark_asset_transport=transport))


def _login_admin(client: TestClient) -> dict[str, str]:
    with client.app.state.db.connect() as connection:
        admin_auth.create_admin(connection, "operator", SecretStr(ADMIN_PASSWORD))
    login = client.post(f"{ADMIN}/login", json={"username": "operator", "password": ADMIN_PASSWORD})
    assert login.status_code == 200, login.text
    return {"X-CSRF-Token": login.json()["csrfToken"]}


def _create_project(client: TestClient, csrf: dict[str, str], email: str) -> dict:
    response = client.post(f"{ADMIN}/projects", headers=csrf, json={"email": email, "password": CUSTOMER_PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def _open_customer(client: TestClient, email: str) -> dict:
    response = client.post(f"{CUSTOMER}/login", json={"email": email, "password": CUSTOMER_PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def _customer_key(client: TestClient, email: str) -> dict:
    customer = _open_customer(client, email)
    response = client.post(
        f"{CUSTOMER}/keys", headers={"X-CSRF-Token": customer["csrfToken"]}, json={"name": "Synthetic API key"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _record_usage(
    connection: sqlite3.Connection, project_id: str, key_id: str, task_id: str, created_at: str,
    output_tokens: int | None, status: str = "succeeded",
) -> None:
    row = connection.execute("""
        INSERT INTO video_tasks (
            id, project_id, key_id, request_hash, model, upstream_model, status, result_json,
            requested_resolution, contains_video_input, output_tokens, created_at, settled_at
        ) VALUES (?, ?, ?, 'synthetic', ?, ?, ?, '{"resolution":"720p"}', '720p', 0, ?, ?, '2025-01-08 03:00:00')
        RETURNING *
    """, (task_id, project_id, key_id, DRAFT_MODEL, UPSTREAM_MODEL, status, output_tokens, created_at)).fetchone()
    billing.record_task(connection, row)


def test_operator_provisions_unique_customers_and_sees_only_metadata_in_project_keys(tmp_path: Path) -> None:
    with _client(tmp_path / "provision.sqlite") as client:
        credentials = {"email": "Customer@example.com", "password": CUSTOMER_PASSWORD}
        assert client.get(f"{ADMIN}/projects").status_code == 401
        assert client.post(f"{CUSTOMER}/register", json=credentials).status_code == 403
        csrf = _login_admin(client)
        assert client.get(f"{CUSTOMER}/me").status_code == 401
        assert client.post(f"{ADMIN}/projects", json=credentials).status_code == 403

        project = _create_project(client, csrf, credentials["email"])
        other = _create_project(client, csrf, "another@example.com")
        assert project["id"] != other["id"]
        assert project["email"] == "customer@example.com"
        assert project["activeKeys"] == project["revokedKeys"] == 0
        assert CUSTOMER_PASSWORD not in str(project)
        assert client.post(f"{ADMIN}/projects", headers=csrf, json=credentials).status_code == 409
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM customer_sessions").fetchone()[0] == 0
            assert connection.execute("SELECT password_hash FROM customer_accounts").fetchone()[0] != CUSTOMER_PASSWORD

        key = _customer_key(client, credentials["email"])
        assert client.get(f"{ADMIN}/projects/{project['id']}/keys").json() == {"keys": [{
            **key["key"], "concurrencyLimit": None, "rpmLimit": None, **EMPTY_KEY_USAGE,
        }]}
        assert client.get(f"{ADMIN}/projects/{other['id']}/keys").json() == {"keys": []}
        search = client.get(f"{ADMIN}/projects", params={"q": "CUSTOMER@EXAMPLE", "pageSize": 1}).json()
        assert search == {"items": [{**project, "activeKeys": 1}], "total": 1, "page": 1, "pageSize": 1}
        assert client.get(f"{ADMIN}/projects/missing/keys").status_code == 404

        audit = client.get(f"{ADMIN}/audit")
        created = [item for item in audit.json()["items"] if item["action"] == "customer.create"]
        assert len(created) == 2
        assert all(item["actor"] == "operator" for item in created)
        assert {item["targetId"] for item in created} == {project["id"], other["id"]}
        for secret in (CUSTOMER_PASSWORD, key["secret"], csrf["X-CSRF-Token"]):
            assert secret not in audit.text

        client.cookies.delete(admin_auth.SESSION_COOKIE)
        assert client.get(f"{ADMIN}/projects").status_code == 401
        assert client.get(
            f"{ADMIN}/projects", headers={"Authorization": f"Bearer {key['secret']}"},
        ).status_code == 401


def test_operator_key_limit_updates_are_scoped_strict_and_audited_once(tmp_path: Path) -> None:
    with _client(tmp_path / "key-limits.sqlite") as client:
        csrf = _login_admin(client)
        project = _create_project(client, csrf, "limits@example.com")
        foreign = _create_project(client, csrf, "foreign@example.com")
        key = _customer_key(client, project["email"])
        key_id = key["key"]["id"]
        keys_url = f"{ADMIN}/projects/{project['id']}/keys"
        url = f"{keys_url}/{key_id}/limits"
        configured = {"concurrencyLimit": 1000, "rpmLimit": 60000}
        unrestricted = {"concurrencyLimit": None, "rpmLimit": None}
        assert client.put(url, json=configured).status_code == 403
        foreign_update = client.put(
            f"{ADMIN}/projects/{foreign['id']}/keys/{key_id}/limits", headers=csrf, json=configured,
        )
        assert foreign_update.status_code == 404
        assert foreign_update.json()["detail"]["code"] == "api_key_not_found"

        for payload in (
            {"concurrencyLimit": 0, "rpmLimit": None}, {"concurrencyLimit": 1001, "rpmLimit": None},
            {"concurrencyLimit": None, "rpmLimit": 0}, {"concurrencyLimit": None, "rpmLimit": 60001},
            {"concurrencyLimit": True, "rpmLimit": None}, {"concurrencyLimit": None, "rpmLimit": "10"},
            {"concurrencyLimit": 1.0, "rpmLimit": None}, {"concurrencyLimit": None},
            {**unrestricted, "projectId": foreign["id"]},
        ):
            assert client.put(url, headers=csrf, json=payload).status_code == 422

        default_key = {**key["key"], **unrestricted, **EMPTY_KEY_USAGE}
        assert client.put(url, headers=csrf, json=unrestricted).json() == default_key
        updated = client.put(url, headers=csrf, json=configured)
        assert updated.status_code == 200
        assert updated.json() == {**default_key, **configured}
        assert key["secret"] not in updated.text
        assert client.put(url, headers=csrf, json=configured).json() == updated.json()
        rpm_only = {"concurrencyLimit": None, "rpmLimit": 60000}
        assert client.put(url, headers=csrf, json=rpm_only).json() == {**default_key, **rpm_only}
        concurrency_only = {"concurrencyLimit": 1000, "rpmLimit": None}
        assert client.put(url, headers=csrf, json=concurrency_only).json() == {**default_key, **concurrency_only}
        assert client.put(url, headers=csrf, json=unrestricted).json() == default_key
        assert client.get(f"{CUSTOMER}/keys").json() == {"keys": [key["key"]]}

        with client.app.state.db.connect() as connection:
            _record_usage(connection, project["id"], key_id, "active_task", "2025-01-01 00:00:00", None, "running")
            now = int(time.time())
            connection.executemany("""
                INSERT INTO general_model_calls (id, project_id, key_id, modality, model, upstream_model, status, created_at)
                VALUES (?, ?, ?, ?, 'fixture', 'fixture', ?, ?)
            """, [
                ("text_live", project["id"], key_id, "text", "in_progress", now),
                ("image_live", project["id"], key_id, "image", "in_progress", now),
                ("image_finished", project["id"], key_id, "image", "succeeded", now),
                ("text_uncertain", project["id"], key_id, "text", "uncertain", now),
                ("image_abandoned", project["id"], key_id, "image", "in_progress", 0),
            ])
            assert key_limits.admit_request(connection, key_id) is None
        observed_key = {
            **default_key, "activeTasks": 1, "activeTextRequests": 1, "activeImageRequests": 1,
            "activeConcurrentRequests": 3, "requestsLastMinute": 1,
        }
        assert client.get(keys_url).json() == {"keys": [observed_key]}

        customer = _open_customer(client, project["email"])
        revoke = client.delete(f"{CUSTOMER}/keys/{key_id}", headers={"X-CSRF-Token": customer["csrfToken"]})
        assert revoke.status_code == 204
        revoked_update = client.put(url, headers=csrf, json=unrestricted)
        assert revoked_update.status_code == 409
        assert revoked_update.json()["detail"]["code"] == "api_key_revoked"
        assert client.get(keys_url).json() == {"keys": [{**observed_key, "status": "revoked"}]}

        audit = client.get(f"{ADMIN}/audit")
        changes = [item for item in audit.json()["items"] if item["action"] == "key.limits.update"]
        assert len(changes) == 4
        for change in changes:
            assert (change["actor"], change["targetType"], change["targetId"]) == ("operator", "key", key_id)
        assert [change["details"] for change in changes] == [
            {"projectId": project["id"], "before": concurrency_only, "after": unrestricted},
            {"projectId": project["id"], "before": rpm_only, "after": concurrency_only},
            {"projectId": project["id"], "before": configured, "after": rpm_only},
            {"projectId": project["id"], "before": unrestricted, "after": configured},
        ]
        assert key["secret"] not in audit.text
        client.cookies.delete(admin_auth.SESSION_COOKIE)
        assert client.put(url, headers=csrf, json=configured).status_code == 401


def test_key_limit_changes_roll_back_if_auditing_fails(tmp_path: Path) -> None:
    with _client(tmp_path / "key-limit-rollback.sqlite") as client:
        csrf = _login_admin(client)
        project = _create_project(client, csrf, "rollback-limits@example.com")
        key_id = _customer_key(client, project["email"])["key"]["id"]
        keys_url = f"{ADMIN}/projects/{project['id']}/keys"
        with client.app.state.db.connect() as connection:
            connection.execute("""
                CREATE TRIGGER reject_limit_audit BEFORE INSERT ON admin_audit
                WHEN NEW.action = 'key.limits.update'
                BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
            client.put(
                f"{keys_url}/{key_id}/limits", headers=csrf, json={"concurrencyLimit": 2, "rpmLimit": 30},
            )
        key = client.get(keys_url).json()["keys"][0]
        assert key["concurrencyLimit"] is None and key["rpmLimit"] is None


def test_admin_usage_and_frozen_bill_reuse_the_customer_ledger(tmp_path: Path) -> None:
    with _client(tmp_path / "operations.sqlite") as client:
        csrf = _login_admin(client)
        project = _create_project(client, csrf, "bill@example.com")
        other = _create_project(client, csrf, "other@example.com")
        project_id = project["id"]
        base = f"{ADMIN}/projects/{project_id}"
        assert client.put(f"{base}/model-access", headers=csrf, json={"seedanceEnabled": True}).status_code == 200
        key = _customer_key(client, project["email"])["key"]["id"]
        other_key = _customer_key(client, other["email"])["key"]["id"]

        price_input = {
            "upstreamModel": UPSTREAM_MODEL, "resolution": "720p", "containsVideoInput": False,
            "effectiveAt": "2024-12-31T00:00:00+08:00", "unitPrice": "1", "source": "Synthetic billing test",
        }
        price = client.post(f"{ADMIN}/prices", headers=csrf, json=price_input)
        assert price.status_code == 200, price.text
        assert price.json()["unitPrice"] == "1.000000"
        assert price.json()["effectiveAt"] == "2024-12-30T16:00:00Z"
        assert client.get(f"{ADMIN}/prices").json()["items"] == [price.json()]
        assert client.post(f"{ADMIN}/prices", headers=csrf, json=price_input).json() == price.json()
        assert client.post(
            f"{ADMIN}/prices", headers=csrf, json={**price_input, "unitPrice": "1.01"},
        ).status_code == 409
        terms_input = {"effectiveAt": price_input["effectiveAt"], "enabled": True, "payableBps": 5000}
        terms = client.post(f"{base}/billing-terms", headers=csrf, json=terms_input)
        assert terms.status_code == 200, terms.text
        assert client.get(f"{base}/billing-terms").json()["items"] == [terms.json()]
        assert client.post(f"{base}/billing-terms", headers=csrf, json=terms_input).json() == terms.json()
        assert client.post(
            f"{base}/billing-terms", headers=csrf, json={**terms_input, "payableBps": 4900},
        ).status_code == 409

        with client.app.state.db.connect() as connection:
            _record_usage(connection, project_id, key, "bill_first", "2025-01-05 00:00:00", 400_000)
            _record_usage(connection, project_id, key, "bill_pending", "2025-01-06 00:00:00", None)
            _record_usage(connection, project_id, key, "bill_failed", "2025-01-07 00:00:00", None, "failed")
            _record_usage(connection, other["id"], other_key, "other_call", "2025-01-07 00:00:00", 90_000)

        page = client.get(f"{base}/usage", params={"month": MONTH, "page": 2, "pageSize": 1})
        assert page.status_code == 200, page.text
        assert page.json()["summary"] == {
            "calls": 3, "successful": 2, "outputTokens": 400_000, "pricedCalls": 1, "pendingCalls": 1,
        }
        assert [item["id"] for item in page.json()["items"]] == ["bill_pending"]
        assert page.json()["items"][0]["billing"]["pendingReason"] == "missing_usage"
        assert "other_call" not in client.get(f"{base}/usage", params={"month": MONTH}).text
        assert client.get(f"{ADMIN}/projects/missing/usage").status_code == 404
        assert client.get(f"{ADMIN}/projects/missing/statements/{MONTH}").status_code == 404

        bill_url = f"{base}/statements/{MONTH}"
        preview = client.get(bill_url).json()
        assert preview["status"] == "estimate"
        assert preview["summary"]["payableAmount"] == "0.20"
        with client.app.state.db.connect() as connection:
            _record_usage(connection, project_id, key, "bill_late", "2025-01-07 02:00:00", 100_000)
        stale = client.post(
            f"{bill_url}/confirm", headers=csrf, json={"expectedPayableAmount": preview["summary"]["payableAmount"]},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "statement_amount_changed"
        assert client.get(bill_url).json()["status"] == "estimate"
        assert client.post(f"{bill_url}/confirm").status_code == 403
        current_month = datetime.now(statements.BEIJING).strftime("%Y-%m")
        open_month = client.post(
            f"{base}/statements/{current_month}/confirm", headers=csrf, json={"expectedPayableAmount": "99.00"},
        )
        assert open_month.status_code == 422
        assert open_month.json()["detail"]["code"] == "cannot_confirm_open_month"

        frozen = client.post(f"{bill_url}/confirm", headers=csrf, json={"expectedPayableAmount": "0.25"})
        assert frozen.status_code == 200, frozen.text
        frozen = frozen.json()
        assert frozen["summary"]["pendingCalls"] == 1
        assert frozen["summary"]["usageCalls"] == 2
        assert frozen["summary"]["payableAmount"] == "0.25"
        assert client.post(f"{bill_url}/confirm", headers=csrf).json() == frozen

        _open_customer(client, project["email"])
        assert client.get(f"{CUSTOMER}/billing", params={"month": MONTH}).json() == frozen
        assert client.get(f"{CUSTOMER}/billing/{frozen['id']}/export.csv").content.startswith(b"\xef\xbb\xbf")
        _open_customer(client, other["email"])
        assert client.get(f"{CUSTOMER}/billing/{frozen['id']}").status_code == 404

        audit = client.get(f"{ADMIN}/audit").json()["items"]
        for action in ("billing.price.create", "billing.terms.create", "billing.statement.confirm"):
            assert sum(item["action"] == action for item in audit) == 1
        assert next(item for item in audit if item["action"] == "billing.statement.confirm")["details"] == {
            "projectId": project_id, "month": MONTH, "payableAmount": "0.25",
        }


def test_admin_ledger_reads_the_real_database_when_customer_demo_is_unavailable(tmp_path: Path) -> None:
    with _client(tmp_path / "demo.sqlite") as client:
        csrf = _login_admin(client)
        project = _create_project(client, csrf, "demo@example.com")
        key = _customer_key(client, project["email"])["key"]["id"]
        with client.app.state.db.connect() as connection:
            _record_usage(connection, project["id"], key, "demo_real", "2025-01-07 00:00:00", 10)
        client.app.state.settings.customer_demo_project_id = project["id"]
        client.app.state.settings.customer_demo_database_path = tmp_path / "missing-demo.sqlite"
        assert client.get(f"{CUSTOMER}/usage", params={"month": MONTH}).status_code == 503
        data = client.get(f"{ADMIN}/projects/{project['id']}/usage", params={"month": MONTH})
        assert data.status_code == 200, data.text
        assert data.json()["items"][0]["id"] == "demo_real"


def test_admin_writes_roll_back_if_auditing_fails(tmp_path: Path) -> None:
    with _client(tmp_path / "rollback.sqlite") as client:
        csrf = _login_admin(client)
        project = _create_project(client, csrf, "first@example.com")
        with client.app.state.db.connect() as connection:
            connection.execute("""
                CREATE TRIGGER fail_audit BEFORE INSERT ON admin_audit
                WHEN NEW.action IN ('customer.create', 'billing.statement.confirm', 'project.model_access.update')
                BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
            """)

        with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
            _create_project(client, csrf, "rollback@example.com")
        response_count = client.get(f"{ADMIN}/projects").json()["total"]
        assert response_count == 1
        bill_url = f"{ADMIN}/projects/{project['id']}/statements/{MONTH}"
        with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
            client.post(f"{bill_url}/confirm", headers=csrf)
        assert client.get(bill_url).json()["status"] == "estimate"
        with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
            client.put(f"{ADMIN}/projects/{project['id']}/model-access", headers=csrf, json={"seedanceEnabled": True})
        assert client.get(f"{ADMIN}/projects").json()["items"][0]["seedanceEnabled"] is False
