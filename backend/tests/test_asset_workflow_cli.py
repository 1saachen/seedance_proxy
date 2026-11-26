from collections.abc import Iterator
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import io
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

from app import billing
from app.accounts import SESSION_COOKIE
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
UPSTREAM_MODEL = "doubao-seedance-2-5-260628"
MONTH = "2025-02"
SETTLED_AT = datetime(2025, 2, 12, 0, 1, tzinfo=UTC)
TOKENS = 123_456


@dataclass(frozen=True)
class Customer:
    project_id: str
    key: str
    session: dict[str, str]

    @property
    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}


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


def _customer(client: httpx.Client, name: str) -> Customer:
    registered = client.post("/api/customer/register", json={"email": f"{name}@example.com", "password": "example-password-123"})
    assert registered.status_code == 201, registered.text
    account = registered.json()
    response = client.post(
        "/api/customer/keys", headers={"X-CSRF-Token": account["csrfToken"]}, json={"name": "CLI acceptance"},
    )
    assert response.status_code == 201
    customer = Customer(
        account["customer"]["projectId"], response.json()["secret"],
        {"Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}"},
    )
    client.cookies.clear()
    return customer


def _environment(**extra: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in ("SYSTEMROOT", "PATH", "TMP", "TEMP") if name in os.environ}
    return env | {"NO_PROXY": "*", "PYTHONIOENCODING": "utf-8"} | extra


def _run_cli(origin: str, image: Path, key: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(EXAMPLE), "--base-url", origin, "--image", str(image),
         "--poll-interval", "0.01", "--asset-timeout", "10"],
        cwd=EXAMPLE.parents[1],
        env=_environment(API_KEY=key),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def _billing_command(database: Path, action: str, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "app.billing_cli", "--database", str(database), action, *args],
        cwd=EXAMPLE.parents[1] / "backend", env=_environment(), capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assert_billing_workflow(client: httpx.Client, database: Path, owner: Customer, other: Customer, task_id: str) -> None:
    task_path = f"{TASKS_PATH}/{task_id}"
    settled = client.get(task_path, headers=owner.bearer)
    assert settled.status_code == 200 and settled.json()["status"] == "succeeded"
    assert settled.json()["usage"]["completion_tokens"] == TOKENS
    assert client.get(task_path, headers=owner.bearer).json() == settled.json()

    usage = client.get("/api/customer/usage", params={"month": MONTH}, headers=owner.session)
    assert usage.status_code == 200
    assert usage.json()["summary"] == {"calls": 1, "successful": 1, "outputTokens": TOKENS, "pricedCalls": 1}
    charge, = usage.json()["items"]
    assert charge["id"] == task_id and charge["settledAt"] == "2025-02-12 00:01:00"
    assert charge["billing"]["status"] == "priced" and charge["billing"]["payableBps"] == 8000
    assert (charge["billing"]["listAmount"], charge["billing"]["amount"]) == ("0.308640", "0.246912")

    billing_path = "/api/customer/billing"
    preview = client.get(billing_path, params={"month": MONTH}, headers=owner.session).json()
    assert preview["status"] == "estimate" and preview["summary"]["payableAmount"] == "0.25"
    assert [line["taskId"] for line in preview["items"]] == [task_id]
    args = ("--project-id", owner.project_id, "--month", MONTH)
    confirmed = _billing_command(database, "confirm-statement", *args)
    statement_id = confirmed["statementId"]
    assert confirmed == {"statementId": statement_id, "month": MONTH, "status": "confirmed", "payableAmount": "0.25"}
    assert _billing_command(database, "confirm-statement", *args) == confirmed
    detail_path = f"{billing_path}/{statement_id}"
    detail = client.get(detail_path, headers=owner.session)
    assert detail.status_code == 200 and detail.json()["items"] == preview["items"]
    export_path = f"{detail_path}/export.csv"
    exported = client.get(export_path, headers=owner.session)
    assert exported.status_code == 200 and exported.content.startswith(b"\xef\xbb\xbf")
    row, = csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))
    assert (row["statementId"], row["taskId"], row["outputTokens"], row["amount"]) == (statement_id, task_id, str(TOKENS), "0.246912")
    for path in (detail_path, export_path):
        denied = client.get(path, headers=other.session)
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "statement_not_found"

    bill = database.with_name("synthetic-provider-bill.json")
    bill.write_text(json.dumps({
        "ResponseMetadata": {"Action": "ListBillDetail"},
        "Result": {"Total": 1, "List": [{
            "BillDetailId": "synthetic-cost-01", "BillPeriod": MONTH, "BusiPeriod": MONTH,
            "PayerID": "1000000001", "OwnerID": "1000000002", "Product": "ark", "Currency": "CNY",
            "ConfigName": UPSTREAM_MODEL, "BillCategory": "consume-use", "BillingMode": "2", "Unit": "token", "Count": str(TOKENS),
            "OriginalBillAmount": "0.162500", "PreferentialBillAmount": "0.012500", "DiscountBillAmount": "0.150000",
            "PayableAmount": "0.150000", "PaidAmount": "0.145000", "CreditCarriedAmount": "0", "UnpaidAmount": "0.005000",
        }]},
    }), encoding="utf-8")
    scope = ("--payer-id", "1000000001", "--owner-id", "1000000002", "--product", "ark", "--month", MONTH)
    imported = _billing_command(database, "import-provider-bill", *scope, "--file", str(bill))
    replay = _billing_command(database, "import-provider-bill", *scope, "--file", str(bill))
    assert (imported["rows"], imported["disposition"]) == (1, "imported")
    assert replay == {**imported, "disposition": "unchanged"}
    report = _billing_command(database, "provider-cost-report", *scope, "--confirm-usage-scope")
    assert report["source"]["importId"] == imported["importId"]
    assert report["customers"] == {"currency": "CNY", "statementCount": 1, "confirmedPayableAmount": "0.25"}
    assert report["provider"]["cashPaidAmount"] == "0.145" and report["provider"]["paymentBalanceDifference"] == "0"
    assert report["usage"] == {
        "scopeConfirmed": True, "unmatchedProviderLines": [], "unmatchedRelayTasks": [],
        "comparisons": [{
            "upstreamModel": UPSTREAM_MODEL, "providerLineCount": 1, "relayTaskCount": 1, "providerTokens": TOKENS,
            "relayKnownTokens": TOKENS, "unknownRelayUsageTasks": 0, "tokenDifference": 0,
        }],
    }
    assert client.get(detail_path, headers=owner.session).json() == detail.json()


def test_cli_asset_video_and_billing_acceptance_over_loopback(tmp_path: Path) -> None:
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
        if request.method == "GET":
            assert request.url.path == f"{TASKS_PATH}/{ARK_TASK_ID}" and calls[-1] == "CreateVideo"
            calls.append("GetVideo")
            return httpx.Response(200, json={
                "id": ARK_TASK_ID, "status": "succeeded", "resolution": "720p", "updated_at": int(SETTLED_AT.timestamp()),
                "content": {"video_url": "https://media.example.com/synthetic.mp4"},
                "usage": {"completion_tokens": TOKENS, "total_tokens": TOKENS},
            })
        assert request.method == "POST" and request.url.path == TASKS_PATH
        assert calls == ["CreateAssetGroup", "CreateAsset", "GetAsset:Processing", "GetAsset:Active"]
        body = json.loads(request.content)
        image_ref, = [item for item in body["content"] if item["type"] == "image_url"]
        assert body["model"] == UPSTREAM_MODEL and image_ref["role"] == "reference_image"
        assert image_ref["image_url"]["url"] == f"asset://{ARK_ASSET_ID}"
        calls.append("CreateVideo")
        return httpx.Response(200, json={"id": ARK_TASK_ID})

    database = tmp_path / "cli.sqlite"
    app = create_app(
        Settings(
            database_path=database, upload_directory=tmp_path / "staging", upload_public_origin=PUBLIC_ORIGIN,
            registration_enabled=True, cookie_secure=False, upload_cleanup_seconds=0, video_poll_seconds=0,
            ark_api_key=ARK_API_KEY, ark_asset_access_key=ARK_ASSET_KEY, ark_asset_secret_key=ARK_ASSET_SECRET,
            ark_asset_project=ARK_PROJECT,
        ),
        ark_transport=httpx.MockTransport(ark_video), ark_asset_transport=httpx.MockTransport(ark_assets),
    )
    with _serve(app) as origin, httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
        owner, other = _customer(client, "alice"), _customer(client, "bob")
        with app.state.db.connect() as connection:
            # Anchor the genuine API-created task to a closed month without inserting a charge or statement fixture.
            connection.execute("""
                CREATE TRIGGER synthetic_past_task AFTER INSERT ON video_tasks
                BEGIN UPDATE video_tasks SET created_at = '2025-02-12 00:00:00' WHERE id = NEW.id; END
            """)
            billing.add_price(
                connection, upstream_model=UPSTREAM_MODEL, resolution="720p", contains_video_input=False,
                effective_at=0, unit_price_micros_per_million=2_500_000, source="Synthetic consolidated acceptance",
            )
            billing.add_project_terms(connection, project_id=owner.project_id, effective_at=0, enabled=True, payable_bps=8000)
        ran = _run_cli(origin, image_path, owner.key)
        if any(value in ran.stdout + ran.stderr for value in (owner.key, ARK_API_KEY, ARK_ASSET_KEY, ARK_ASSET_SECRET,
                                                                ARK_GROUP_ID, ARK_ASSET_ID, ARK_TASK_ID, "/api/asset/source/")):
            pytest.fail("The CLI output exposed confidential provider or account data", pytrace=False)
        assert ran.returncode == 0, ran.stderr
        result = json.loads(ran.stdout)
        assert set(result) == {"uploadId", "assetId", "reference", "taskId"}
        assert result["uploadId"].startswith("upl_")
        assert result["assetId"].startswith("ast_") and result["reference"] == f"asset://{result['assetId']}"
        assert result["taskId"].startswith("vid_")

        available = client.get("/api/asset/get", params={"assetId": result["assetId"]}, headers=owner.bearer)
        assert available.status_code == 200 and available.json()["asset"]["reference"] == result["reference"]
        headers = other.bearer
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

        _assert_billing_workflow(client, database, owner, other, result["taskId"])
        invalid_key = "test-only-invalid-business-key"
        failed = _run_cli(origin, image_path, invalid_key)
        assert failed.returncode != 0 and failed.stdout == ""
        if invalid_key in failed.stderr:
            pytest.fail("The CLI failure exposed its input credential", pytrace=False)
        assert "create-group failed (HTTP 401, invalid_api_key)" in failed.stderr
        assert calls == ["CreateAssetGroup", "CreateAsset", "GetAsset:Processing", "GetAsset:Active", "CreateVideo", "GetVideo"]
