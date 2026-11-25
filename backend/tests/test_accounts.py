import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.main import create_app


CUSTOMER_PATH = "/api/customer"
PASSWORD = "example-password-123"


def _register(client: TestClient, email: str):
    return client.post(f"{CUSTOMER_PATH}/register", json={"email": email, "password": PASSWORD})


def _client(path: Path, *, registration_enabled: bool = True, cookie_secure: bool = False):
    settings = Settings(
        database_path=path, registration_enabled=registration_enabled, cookie_secure=cookie_secure, ark_api_key=None,
    )
    return TestClient(create_app(settings), base_url="https://testserver" if cookie_secure else "http://testserver")


def test_disabled_registration_and_anonymous_access_are_rejected(tmp_path: Path) -> None:
    with _client(tmp_path / "disabled.sqlite", registration_enabled=False) as client:
        response = _register(client, "alice@example.com")
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "registration_disabled"
        assert client.get(f"{CUSTOMER_PATH}/me").status_code == 401
        assert client.get(f"{CUSTOMER_PATH}/keys").status_code == 401
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_registration_creates_one_project_and_stores_only_hashes(tmp_path: Path) -> None:
    with _client(tmp_path / "registration.sqlite", cookie_secure=True) as client:
        response = _register(client, " Alice@Example.COM ")
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie and "Secure" in set_cookie
        assert "Path=/api/customer" in set_cookie and "SameSite=strict" in set_cookie
        session = response.cookies[SESSION_COOKIE]
        account = response.json()
        assert account["customer"]["email"] == "alice@example.com"
        assert client.get(f"{CUSTOMER_PATH}/me").json() == account
        assert _register(client, "alice@example.com").status_code == 409

        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
            project = connection.execute("SELECT id FROM projects").fetchone()
            assert project["id"] == account["customer"]["projectId"]
            saved_password = connection.execute("SELECT password_hash FROM customer_accounts").fetchone()[0]
            saved_session = connection.execute("SELECT token_hash FROM customer_sessions").fetchone()[0]
        assert saved_password.startswith("scrypt-v1$") and PASSWORD not in saved_password
        assert saved_session == hashlib.sha256(session.encode()).hexdigest()
        assert session not in response.text and PASSWORD not in response.text


def test_login_rotates_session_and_logout_requires_csrf(tmp_path: Path) -> None:
    with _client(tmp_path / "session.sqlite") as client:
        registration = _register(client, "alice@example.com")
        old_session = registration.cookies[SESSION_COOKIE]
        old_csrf = registration.json()["csrfToken"]
        failures = [
            client.post(f"{CUSTOMER_PATH}/login", json={"email": email, "password": "incorrect-password"})
            for email in ("alice@example.com", "unknown@example.com")
        ]
        assert [item.status_code for item in failures] == [401, 401]
        assert failures[0].json() == failures[1].json()

        login = client.post(f"{CUSTOMER_PATH}/login", json={"email": "alice@example.com", "password": PASSWORD})
        assert login.status_code == 200
        new_csrf = login.json()["csrfToken"]
        assert login.cookies[SESSION_COOKIE] != old_session and new_csrf != old_csrf
        old_me = client.get(f"{CUSTOMER_PATH}/me", headers={"Cookie": f"{SESSION_COOKIE}={old_session}"})
        assert old_me.status_code == 401
        assert client.post(f"{CUSTOMER_PATH}/logout", headers={"X-CSRF-Token": old_csrf}).status_code == 403
        assert client.post(f"{CUSTOMER_PATH}/logout", headers={"X-CSRF-Token": new_csrf}).status_code == 204
        assert client.get(f"{CUSTOMER_PATH}/me").status_code == 401


def test_project_key_creation_isolated_revocable_and_never_listed_in_full(tmp_path: Path) -> None:
    with _client(tmp_path / "keys.sqlite") as client:
        alice = _register(client, "alice@example.com").json()
        alice_token = client.cookies[SESSION_COOKIE]
        alice_csrf = {"X-CSRF-Token": alice["csrfToken"]}
        assert client.post(f"{CUSTOMER_PATH}/keys", json={"name": "first"}).status_code == 403
        response = client.post(f"{CUSTOMER_PATH}/keys", headers=alice_csrf, json={"name": "first"})
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        issued = response.json()
        key_id = issued["key"]["id"]
        secret = issued["secret"]
        assert secret.startswith("vap_live_")
        headers = {"Authorization": f"Bearer {secret}"}

        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers=headers).status_code == 200
        listing = client.get(f"{CUSTOMER_PATH}/keys")
        assert listing.json() == {"keys": [issued["key"]]}
        assert secret not in listing.text
        with client.app.state.db.connect() as connection:
            saved = connection.execute("SELECT project_id, secret_hash FROM business_keys").fetchone()
            assert saved["project_id"] == alice["customer"]["projectId"]
            assert saved["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()

        client.cookies.clear()
        assert client.get(f"{CUSTOMER_PATH}/me", headers=headers).status_code == 401
        bob = _register(client, "bob@example.com").json()
        assert bob["customer"]["projectId"] != alice["customer"]["projectId"]
        assert client.get(f"{CUSTOMER_PATH}/keys").json() == {"keys": []}
        forbidden = client.delete(f"{CUSTOMER_PATH}/keys/{key_id}", headers={"X-CSRF-Token": bob["csrfToken"]})
        assert forbidden.status_code == 404
        assert client.get("/v1/models", headers=headers).status_code == 200

        revoke_headers = {**alice_csrf, "Cookie": f"{SESSION_COOKIE}={alice_token}"}
        assert client.delete(f"{CUSTOMER_PATH}/keys/{key_id}", headers=revoke_headers).status_code == 204
        assert client.get("/v1/models", headers=headers).status_code == 401
        revoked = client.get(f"{CUSTOMER_PATH}/keys", headers=revoke_headers).json()["keys"][0]
        assert revoked["status"] == "revoked"


def test_expired_session_and_malformed_credentials_are_safe(tmp_path: Path) -> None:
    with _client(tmp_path / "expired.sqlite") as client:
        response = client.post(f"{CUSTOMER_PATH}/register", json={"email": [], "password": {"secret": PASSWORD}})
        assert response.status_code == 422 and PASSWORD not in response.text
        assert _register(client, "alice@example.com").status_code == 201
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE customer_sessions SET expires_at = 0")
        assert client.get(f"{CUSTOMER_PATH}/me").status_code == 401


def test_login_rate_limit_rejects_ninth_attempt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.accounts.time.time", lambda: 1800)
    with _client(tmp_path / "limits.sqlite") as client:
        payload = {"email": "alice@example.com", "password": PASSWORD}
        for _ in range(8):
            assert client.post(f"{CUSTOMER_PATH}/login", json=payload).status_code == 401
        blocked = client.post(f"{CUSTOMER_PATH}/login", json=payload)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "900"
        assert blocked.json()["detail"]["code"] == "auth_rate_limited"
