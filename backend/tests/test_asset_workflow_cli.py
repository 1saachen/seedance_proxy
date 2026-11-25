from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.model_api import TASKS_PATH


EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "asset_video.py"
ARK_PROJECT = "test-shared-ark-project"
ARK_API_KEY = "test-only-inference-key"
ARK_ASSET_KEY = "test-only-asset-key"
ARK_ASSET_SECRET = "test-only-asset-secret"
ARK_GROUP_ID = "group-provider-01"
ARK_ASSET_ID = "asset-provider-01"
ARK_TASK_ID = "provider-task-01"
PUBLIC_ORIGIN = "https://relay.example.com"


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, access_log=False, lifespan="on", loop="asyncio", http="h11", ws="none",
            workers=1, timeout_graceful_shutdown=2,
        ))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    pytest.fail("The local API server did not start")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "The local API server did not stop"


def _key(client: httpx.Client, name: str) -> str:
    registered = client.post("/api/customer/register", json={"email": f"{name}@example.com", "password": "example-password-123"})
    assert registered.status_code == 201, registered.text
    response = client.post(
        "/api/customer/keys", headers={"X-CSRF-Token": registered.json()["csrfToken"]}, json={"name": "CLI acceptance"},
    )
    assert response.status_code == 201
    return response.json()["secret"]


def _run_cli(origin: str, image: Path, key: str) -> subprocess.CompletedProcess[str]:
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH", "TMP", "TEMP") if name in os.environ}
    env.update(API_KEY=key, NO_PROXY="*", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(EXAMPLE), "--base-url", origin, "--image", str(image),
         "--poll-interval", "0.01", "--asset-timeout", "10"],
        cwd=EXAMPLE.parents[1],
        env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_cli_upload_register_poll_and_submit_over_loopback(tmp_path: Path) -> None:
    image_path = tmp_path / "portrait.png"
    with Image.new("RGB", (320, 320), color="#309c81") as image:
        image.save(image_path)
    image_bytes = image_path.read_bytes()
    calls: list[str] = []

    async def ark_assets(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        body = json.loads(request.content)
        assert body["ProjectName"] == ARK_PROJECT
        if action == "CreateAssetGroup":
            calls.append(action)
            return httpx.Response(200, json={"Result": {"Id": ARK_GROUP_ID}})
        if action == "CreateAsset":
            calls.append(action)
            assert body["GroupId"] == ARK_GROUP_ID and body["AssetType"] == "Image"
            source = httpx.URL(body["URL"])
            assert str(source).startswith(f"{PUBLIC_ORIGIN}/api/asset/source/") and source.path.endswith(".png")
            # Simulate Ark retrieving the configured HTTPS URL through the same ASGI application.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
                fetched = await client.get(source)
            assert fetched.status_code == 200 and fetched.content == image_bytes
            assert fetched.headers["Content-Type"] == "image/png"
            return httpx.Response(200, json={"Result": {"Id": ARK_ASSET_ID}})
        assert action == "GetAsset" and body["Id"] == ARK_ASSET_ID
        status = "Processing" if "GetAsset:Processing" not in calls else "Active"
        calls.append(f"GetAsset:{status}")
        return httpx.Response(200, json={"Result": {
            "Id": ARK_ASSET_ID, "GroupId": ARK_GROUP_ID, "ProjectName": ARK_PROJECT,
            "AssetType": "Image", "Status": status,
        }})

    def ark_video(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == TASKS_PATH
        assert calls == ["CreateAssetGroup", "CreateAsset", "GetAsset:Processing", "GetAsset:Active"]
        body = json.loads(request.content)
        image_ref, = [item for item in body["content"] if item["type"] == "image_url"]
        assert body["model"] == "doubao-seedance-2-5-260628" and image_ref["role"] == "reference_image"
        assert image_ref["image_url"]["url"] == f"asset://{ARK_ASSET_ID}"
        calls.append("CreateVideo")
        return httpx.Response(200, json={"id": ARK_TASK_ID})

    app = create_app(
        Settings(
            database_path=tmp_path / "cli.sqlite", upload_directory=tmp_path / "staging", upload_public_origin=PUBLIC_ORIGIN,
            registration_enabled=True, cookie_secure=False, upload_cleanup_seconds=0, video_poll_seconds=0,
            ark_api_key=ARK_API_KEY, ark_asset_access_key=ARK_ASSET_KEY, ark_asset_secret_key=ARK_ASSET_SECRET,
            ark_asset_project=ARK_PROJECT,
        ),
        ark_transport=httpx.MockTransport(ark_video), ark_asset_transport=httpx.MockTransport(ark_assets),
    )
    with _serve(app) as origin, httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
        owner_key, other_key = _key(client, "alice"), _key(client, "bob")
        client.cookies.clear()
        ran = _run_cli(origin, image_path, owner_key)
        if any(value in ran.stdout + ran.stderr for value in (owner_key, ARK_API_KEY, ARK_ASSET_KEY, ARK_ASSET_SECRET,
                                                                ARK_GROUP_ID, ARK_ASSET_ID, ARK_TASK_ID, "/api/asset/source/")):
            pytest.fail("The CLI output exposed confidential provider or account data", pytrace=False)
        assert ran.returncode == 0, ran.stderr
        result = json.loads(ran.stdout)
        assert set(result) == {"uploadId", "assetId", "reference", "taskId"}
        assert result["uploadId"].startswith("upl_")
        assert result["assetId"].startswith("ast_") and result["reference"] == f"asset://{result['assetId']}"
        assert result["taskId"].startswith("vid_")

        owner = {"Authorization": f"Bearer {owner_key}"}
        available = client.get("/api/asset/get", params={"assetId": result["assetId"]}, headers=owner)
        assert available.status_code == 200 and available.json()["asset"]["reference"] == result["reference"]
        headers = {"Authorization": f"Bearer {other_key}"}
        assert client.get(f"{TASKS_PATH}/{result['taskId']}", headers=headers).status_code == 404
        assert client.get("/api/asset/list", headers=headers).json() == {"assets": []}
        missing_upload = client.get(f"/api/asset/upload-file/{result['uploadId']}", headers=headers)
        missing_asset = client.get("/api/asset/get", params={"assetId": result["assetId"]}, headers=headers)
        foreign_reference = client.post(TASKS_PATH, headers=headers, json={
            "model": "doubao-seedance-2.0", "content": [{
                "type": "image_url", "image_url": {"url": result["reference"]}, "role": "reference_image",
            }],
        })
        for response, code in ((missing_upload, "upload_not_found"), (missing_asset, "asset_not_found"),
                               (foreign_reference, "asset_not_found")):
            assert response.status_code == 404, response.text
            assert response.json()["detail"]["code"] == code

        invalid_key = "test-only-invalid-business-key"
        failed = _run_cli(origin, image_path, invalid_key)
        assert failed.returncode != 0 and failed.stdout == ""
        if invalid_key in failed.stderr:
            pytest.fail("The CLI failure exposed its input credential", pytrace=False)
        assert "create-group failed (HTTP 401, invalid_api_key)" in failed.stderr
        assert calls == ["CreateAssetGroup", "CreateAsset", "GetAsset:Processing", "GetAsset:Active", "CreateVideo"]
