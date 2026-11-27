import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import accounts, admin_auth, admin_cli
from app.config import Settings
from app.db import Database
from app.main import create_app


ADMIN_PATH = "/api/admin"
CUSTOMER_PATH = "/api/customer"
PASSWORD = "synthetic-admin-passphrase"


def _no_upstream(_: httpx.Request) -> httpx.Response:
    raise AssertionError("Authentication must not call an upstream")


def _client(path: Path) -> TestClient:
    settings = Settings(
        database_path=path,
        registration_enabled=True,
        cookie_secure=True,
        ark_api_key=None,
        video_poll_seconds=0,
        video_callback_poll_seconds=0,
        upload_cleanup_seconds=0,
    )
    transport = httpx.MockTransport(_no_upstream)
    app = create_app(settings, ark_transport=transport, ark_asset_transport=transport)
    return TestClient(app, base_url="https://testserver")


def _provision(client: TestClient) -> admin_auth.AdminPrincipal:
    with client.app.state.db.connect() as connection:
        return admin_auth.create_admin(connection, " OperaTor ", SecretStr(PASSWORD))


def _login(client: TestClient, username: str = "operator", password: str = PASSWORD):
    return client.post(f"{ADMIN_PATH}/login", json={"username": username, "password": password})


def test_admin_session_is_independent_from_customer_credentials(tmp_path: Path) -> None:
    with _client(tmp_path / "separate.sqlite") as client:
        admin = _provision(client)
        registration = client.post(
            f"{CUSTOMER_PATH}/register", json={"email": "customer@example.com", "password": PASSWORD},
        )
        assert registration.status_code == 201
        assert client.get(f"{ADMIN_PATH}/me").status_code == 401
        customer_session = registration.cookies[accounts.SESSION_COOKIE]
        impersonation = client.get(
            f"{ADMIN_PATH}/me", headers={"Cookie": f"{admin_auth.SESSION_COOKIE}={customer_session}"},
        )
        assert impersonation.status_code == 401
        assert _login(client, "customer@example.com").status_code == 401

        key = client.post(
            f"{CUSTOMER_PATH}/keys", json={"name": "test"},
            headers={"X-CSRF-Token": registration.json()["csrfToken"]},
        )
        assert key.status_code == 201
        bearer = {"Authorization": f"Bearer {key.json()['secret']}"}
        assert client.get(f"{ADMIN_PATH}/me", headers=bearer).status_code == 401

        login = _login(client, " Operator ")
        assert login.status_code == 200
        assert login.headers["Cache-Control"] == "no-store"
        cookie = login.headers["Set-Cookie"]
        assert {"HttpOnly", "Secure", "SameSite=strict", "Path=/api/admin"}.issubset(cookie.split("; "))
        token = login.cookies[admin_auth.SESSION_COOKIE]
        payload = login.json()
        assert payload["username"] == admin.username
        assert client.get(f"{ADMIN_PATH}/me").json() == payload
        assert token not in login.text and PASSWORD not in login.text
        assert client.get("/v1/models").status_code == 401
        impersonation = client.get(
            f"{CUSTOMER_PATH}/me", headers={"Cookie": f"{accounts.SESSION_COOKIE}={token}"},
        )
        assert impersonation.status_code == 401

        with client.app.state.db.connect() as connection:
            password_hash = connection.execute("SELECT password_hash FROM admin_accounts").fetchone()[0]
            session_hash = connection.execute("SELECT token_hash FROM admin_sessions").fetchone()[0]
        assert password_hash.startswith("scrypt-v1$") and PASSWORD not in password_hash
        assert session_hash == hashlib.sha256(token.encode()).hexdigest()


def test_admin_login_rotation_csrf_logout_and_expiration(tmp_path: Path, monkeypatch) -> None:
    now = 1800
    monkeypatch.setattr(admin_auth, "time", SimpleNamespace(time=lambda: now))
    with _client(tmp_path / "sessions.sqlite") as client:
        _provision(client)
        assert client.get(f"{ADMIN_PATH}/audit").status_code == 401
        first = _login(client)
        assert first.status_code == 200
        previous_token = first.cookies[admin_auth.SESSION_COOKIE]
        second = _login(client)
        assert second.status_code == 200
        assert second.cookies[admin_auth.SESSION_COOKIE] != previous_token
        assert client.get(
            f"{ADMIN_PATH}/me", headers={"Cookie": f"{admin_auth.SESSION_COOKIE}={previous_token}"},
        ).status_code == 401
        assert client.post(f"{ADMIN_PATH}/logout").status_code == 403
        assert client.post(
            f"{ADMIN_PATH}/logout", headers={"X-CSRF-Token": first.json()["csrfToken"]},
        ).status_code == 403
        assert client.post(
            f"{ADMIN_PATH}/logout", headers={"X-CSRF-Token": second.json()["csrfToken"]},
        ).status_code == 204
        assert client.get(f"{ADMIN_PATH}/me").status_code == 401

        assert _login(client).status_code == 200
        now += admin_auth.SESSION_SECONDS
        assert client.get(f"{ADMIN_PATH}/me").status_code == 401
        assert _login(client).status_code == 200
        audit = client.get(f"{ADMIN_PATH}/audit?pageSize=2").json()
        assert audit["total"] == 6 and audit["page"] == 1 and audit["pageSize"] == 2
        assert [item["action"] for item in audit["items"]] == ["auth.login", "auth.login"]
        next_page = client.get(f"{ADMIN_PATH}/audit?page=2&pageSize=2").json()
        assert [item["action"] for item in next_page["items"]] == ["auth.logout", "auth.login"]
        assert next_page["items"][0]["actor"] == "operator"
        assert set(next_page["items"][0]) == {"id", "actor", "action", "targetType", "targetId", "details", "createdAt"}
        assert client.get(f"{ADMIN_PATH}/audit?pageSize=101").status_code == 422


def test_login_requires_json_and_failures_are_throttled_and_sanitized(tmp_path: Path, monkeypatch) -> None:
    now = 1800
    monkeypatch.setattr(admin_auth, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(admin_auth, "ADDRESS_LIMIT", 4)
    monkeypatch.setattr(admin_auth, "USERNAME_LIMIT", 2)
    with _client(tmp_path / "throttle.sqlite") as client:
        _provision(client)
        no_type = client.post(
            f"{ADMIN_PATH}/login", content=json.dumps({"username": "operator", "password": PASSWORD}),
        )
        assert no_type.status_code == 415
        assert client.get(f"{ADMIN_PATH}/me").status_code == 401

        unknown = "sensitive-value-as-username"
        bad_known = _login(client, password="incorrect-secret-value")
        bad_unknown = _login(client, username=unknown)
        assert bad_known.status_code == bad_unknown.status_code == 401
        assert bad_known.json() == bad_unknown.json()
        assert _login(client, password="incorrect-secret-value").status_code == 401
        blocked_name = _login(client)
        assert blocked_name.status_code == 429
        assert blocked_name.headers["Retry-After"] == str(admin_auth.LOGIN_WINDOW_SECONDS)
        assert _login(client, username="different").status_code == 429
        assert _login(client, username="and-another").status_code == 429

        now += admin_auth.LOGIN_WINDOW_SECONDS
        login = _login(client)
        assert login.status_code == 200
        audit = client.get(f"{ADMIN_PATH}/audit")
        assert audit.status_code == 200
        assert audit.headers["Cache-Control"] == "no-store"
        assert [item["action"] for item in audit.json()["items"]] == [
            "auth.login", "auth.login_throttled", "auth.login_throttled", "auth.login_failed",
            "auth.login_failed", "auth.login_failed", "admin.created",
        ]
        for secret in (unknown, "incorrect-secret-value", PASSWORD, login.json()["csrfToken"],
                       login.cookies[admin_auth.SESSION_COOKIE]):
            assert secret not in audit.text


def test_offline_admin_creation_validates_the_password_and_preserves_existing_account(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    path = tmp_path / "cli.sqlite"
    monkeypatch.setattr(sys, "argv", ["admin_cli", "--database", str(path), "create-admin", "Operator"])
    assert admin_cli.main() == 1
    assert not path.exists()
    with Database(path).connect() as connection:
        accounts.initialize(connection)

    mismatched = iter((PASSWORD, "different-synthetic-passphrase"))
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda _: next(mismatched))
    assert admin_cli.main() == 1
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda _: "too-short")
    assert admin_cli.main() == 1
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda _: PASSWORD)
    assert admin_cli.main() == 0
    monkeypatch.setattr(admin_cli.getpass, "getpass", lambda _: "different-synthetic-passphrase")
    assert admin_cli.main() == 1
    out, err = capsys.readouterr()
    assert "Created administrator operator." in out
    assert "Passwords do not match" in err and "12–128" in err and "already exists" in err
    assert PASSWORD not in out + err and "different-synthetic-passphrase" not in out + err
    with _client(path) as client:
        assert _login(client).status_code == 200
        assert _login(client, password="different-synthetic-passphrase").status_code == 401
        audit = client.get(f"{ADMIN_PATH}/audit").json()
        assert [item["action"] for item in audit["items"]].count("admin.created") == 1
