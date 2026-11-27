import json

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr

from app import accounts, admin_auth, model_access
from app.config import Settings
from app.main import create_app
from app.model_api import TASKS_PATH


def _customer(client, name):
    client.cookies.delete(accounts.SESSION_COOKIE)
    joined = client.post("/api/customer/register", json={"email": f"{name}@example.test", "password": "example-customer"})
    assert joined.status_code == 201
    profile = joined.json()
    assert profile["capabilities"] == {"seedance": False}
    session = {"Cookie": f"{accounts.SESSION_COOKIE}={joined.cookies[accounts.SESSION_COOKIE]}"}
    key = client.post("/api/customer/keys", json={"name": "test"}, headers={**session, "X-CSRF-Token": profile["csrfToken"]})
    assert key.status_code == 201
    return profile["customer"]["projectId"], session, {"Authorization": f"Bearer {key.json()['secret']}"}


def test_seedance_requires_an_explicit_project_grant_and_revocation_uses_existing_keys(tmp_path):
    upstream_requests = []

    def provider(request):
        assert request.method == "POST" and request.url.path == TASKS_PATH
        upstream_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test-only-provider-id"})

    transport = httpx.MockTransport(provider)
    app = create_app(Settings(
        database_path=tmp_path / "access.sqlite", registration_enabled=True, cookie_secure=False,
        ark_api_key="synthetic-test", video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
    ), ark_transport=transport, ark_asset_transport=transport)

    with TestClient(app) as client:
        alice, alice_session, alice_key = _customer(client, "alice")
        bob, bob_session, bob_key = _customer(client, "bob")
        video = {"model": "doubao-seedance-2.5", "content": [{"type": "text", "text": "Quiet shoreline"}]}
        public = client.get("/v1/models", headers=alice_key)
        assert public.status_code == 200 and public.headers["Cache-Control"] == "no-store"
        assert {model["type"] for model in public.json()["data"]} == {"text", "image"}
        assert "seedance" not in public.text.lower()

        protected_requests = (
            ("POST", TASKS_PATH, video), ("GET", TASKS_PATH, None),
            ("GET", TASKS_PATH + "/vid_sample", None), ("DELETE", TASKS_PATH + "/vid_sample", None),
            ("POST", "/api/asset/upload-file", None), ("GET", "/api/asset/upload-file/upl_sample", None),
            ("GET", "/api/asset/list", None), ("GET", "/api/asset/presets", None),
            ("GET", "/api/asset-group/list", None), ("POST", "/api/asset-group/create", {"name": "Forbidden"}),
            ("POST", "/api/asset-group/liveness/start", {"callbackUrl": "https://example.com/callback"}),
        )
        for method, path, body in protected_requests:
            denied = client.request(method, path, headers=alice_key, json=body)
            assert denied.status_code == 403 and denied.json()["detail"]["code"] == "seedance_access_required", path
        assert client.get("/api/customer/tasks", headers=alice_session).status_code == 403
        assert client.get("/api/customer/playground/key", headers={**alice_session, **alice_key}).status_code == 204
        assert upstream_requests == []

        admin_path = f"/api/admin/projects/{alice}/model-access"
        assert client.put(admin_path, headers=alice_session, json={"seedanceEnabled": True}).status_code == 401
        assert client.put(admin_path, headers=alice_key, json={"seedanceEnabled": True}).status_code == 401
        with app.state.db.connect() as connection:
            admin_auth.create_admin(connection, "operator", SecretStr("example-admin-password"))
        login = client.post("/api/admin/login", json={"username": "operator", "password": "example-admin-password"})
        assert login.status_code == 200
        csrf = {"X-CSRF-Token": login.json()["csrfToken"]}
        assert client.put(admin_path, json={"seedanceEnabled": True}).status_code == 403
        assert client.put(admin_path, headers=csrf, json={"seedanceEnabled": "true"}).status_code == 422
        for _ in range(2):
            assert client.put(admin_path, headers=csrf, json={"seedanceEnabled": True}).json() == {"seedanceEnabled": True}

        summaries = client.get("/api/admin/projects").json()["items"]
        assert {item["id"]: item["seedanceEnabled"] for item in summaries} == {alice: True, bob: False}
        assert client.get("/api/customer/me", headers=alice_session).json()["capabilities"] == {"seedance": True}
        assert client.get("/api/customer/me", headers=bob_session).json()["capabilities"] == {"seedance": False}
        assert client.get("/v1/models", headers=bob_key).json() == public.json()
        granted = client.get("/v1/models", headers=alice_key).json()["data"]
        assert any(model["type"] == "video" and model["id"] == video["model"] for model in granted)
        assert client.get("/api/customer/tasks", headers=alice_session).status_code == 200
        assert client.get("/api/asset/list", headers=alice_key).status_code == 200
        created = client.post(TASKS_PATH, json=video, headers=alice_key)
        assert created.status_code == 200 and len(upstream_requests) == 1
        assert client.post(TASKS_PATH, json=video, headers=bob_key).status_code == 403

        assert client.put(admin_path, headers=csrf, json={"seedanceEnabled": False}).json() == {"seedanceEnabled": False}
        assert client.get("/v1/models", headers=alice_key).json() == public.json()
        assert client.get("/api/customer/me", headers=alice_session).json()["capabilities"] == {"seedance": False}
        assert client.get(f"{TASKS_PATH}/{created.json()['id']}", headers=alice_key).status_code == 403
        assert client.get("/api/asset/list", headers=alice_key).status_code == 403
        assert client.post(TASKS_PATH, json=video, headers=alice_key).status_code == 403
        assert len(upstream_requests) == 1
        audit = [entry for entry in client.get("/api/admin/audit").json()["items"] if entry["action"] == "project.model_access.update"]
        assert len(audit) == 2 and {entry["targetId"] for entry in audit} == {alice}
        with app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_tasks WHERE project_id = ?", (alice,)).fetchone()[0] == 1
            model_access.initialize(connection)
            assert not model_access.has_seedance_access(connection, alice)
