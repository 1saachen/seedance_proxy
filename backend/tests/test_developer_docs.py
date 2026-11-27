import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access


VIDEO_MANUAL = "/api/customer/docs/video"


def test_video_manual_and_schema_require_a_live_project_grant(tmp_path):
    def no_provider_calls(request):
        raise AssertionError(f"Documentation attempted upstream {request.method}")

    transport = httpx.MockTransport(no_provider_calls)
    with TestClient(create_app(Settings(
        database_path=tmp_path / "manual.sqlite", registration_enabled=True, cookie_secure=False,
        video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
    ), ark_transport=transport, ark_asset_transport=transport)) as client:
        public = client.get("/openapi.json")
        assert public.status_code == 200
        public_paths = public.json()["paths"]
        assert set(public_paths) == {"/v1/models", "/v1/chat/completions", "/v1/images/generations"}
        for path in ("/v1/chat/completions", "/v1/images/generations"):
            assert "concurrency limit" in public_paths[path]["post"]["responses"]["429"]["description"]
        assert "seedance" not in public.text.lower()
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404

        for path in (VIDEO_MANUAL, VIDEO_MANUAL + "/openapi.json"):
            anonymous = client.get(path)
            assert anonymous.status_code == 401
            assert anonymous.headers["x-robots-tag"] == "noindex, nofollow"
            assert anonymous.headers["cache-control"] == "no-store"
            assert "Seedance API" not in anonymous.text

        registration = client.post("/api/customer/register", json={
            "email": "manual@example.test", "password": "example-only-password",
        })
        assert registration.status_code == 201
        project_id = registration.json()["customer"]["projectId"]
        assert client.get(VIDEO_MANUAL).status_code == 403
        assert client.get(VIDEO_MANUAL + "/openapi.json").status_code == 403
        with client.app.state.db.connect() as connection:
            set_seedance_access(connection, project_id, True)

        response = client.get(VIDEO_MANUAL)
        assert response.status_code == 200
        assert "Seedance API" in response.text and "/docs-assets/reference.css" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"].startswith("default-src 'none'")
        video_schema = client.get(VIDEO_MANUAL + "/openapi.json").json()["paths"]
        assert "/api/v3/contents/generations/tasks" in video_schema
        assert "/api/asset/upload-file" in video_schema
        assert not any(path.startswith("/api/admin/") for path in video_schema)

        with client.app.state.db.connect() as connection:
            set_seedance_access(connection, project_id, False)
        assert client.get(VIDEO_MANUAL).status_code == 403
        assert client.get(VIDEO_MANUAL + "/openapi.json").status_code == 403
