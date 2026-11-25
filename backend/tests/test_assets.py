import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.assets import ARK_ASSET_HOST, translate_reference
from app.config import Settings
from app.main import create_app


ASSET_GROUP_CREATE = "/api/asset-group/create"
ASSET_GROUP_LIST = "/api/asset-group/list"
ASSET_CREATE = "/api/asset/create"
ASSET_GET = "/api/asset/get"
ASSET_LIST = "/api/asset/list"
ARK_PROJECT = "shared-ark-project"
ACCESS_KEY = "example-asset-access-key"
SECRET_KEY = "example-asset-secret-key"
SOURCE = "https://media.example.com/reference.png?token=example-private-token"


def _client(path: Path, handler, *, credentials: bool = True) -> TestClient:
    settings = Settings(
        database_path=path,
        registration_enabled=True,
        cookie_secure=False,
        ark_api_key=None,
        ark_asset_access_key=SecretStr(ACCESS_KEY) if credentials else None,
        ark_asset_secret_key=SecretStr(SECRET_KEY) if credentials else None,
        ark_asset_project=ARK_PROJECT,
    )
    return TestClient(create_app(settings, ark_asset_transport=httpx.MockTransport(handler)))


def _project(client: TestClient, email: str) -> tuple[dict, str]:
    signup = client.post("/api/customer/register", json={"email": email, "password": "example-password-123"})
    assert signup.status_code == 201
    account = signup.json()
    key = client.post("/api/customer/keys", json={"name": "test"}, headers={"X-CSRF-Token": account["csrfToken"]})
    assert key.status_code == 201
    return {"Authorization": f"Bearer {key.json()['secret']}"}, account["customer"]["projectId"]


def _assert_provider_request(request: httpx.Request, action: str, body: dict) -> None:
    assert request.method == "POST" and request.url.host == ARK_ASSET_HOST and request.url.path == "/"
    assert request.url.query == f"Action={action}&Version=2024-01-01".encode()
    assert json.loads(request.content) == {**body, "ProjectName": ARK_PROJECT}
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-content-sha256"] == hashlib.sha256(request.content).hexdigest()
    stamp = request.headers["x-date"]
    assert re.fullmatch(r"\d{8}T\d{6}Z", stamp)
    assert re.fullmatch(
        rf"HMAC-SHA256 Credential={ACCESS_KEY}/{stamp[:8]}/cn-beijing/ark/request, "
        "SignedHeaders=content-type;host;x-content-sha256;x-date, Signature=[0-9a-f]{64}",
        request.headers["authorization"],
    )
    assert SECRET_KEY not in str(request.headers)


def _asset_body(group_id: str) -> dict:
    return {"groupId": group_id, "name": "portrait", "assetType": "Image", "url": SOURCE}


def test_asset_mapping_is_tenant_owned_and_translates_only_active_material(tmp_path: Path) -> None:
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        calls.append(action)
        if action == "CreateAssetGroup":
            _assert_provider_request(request, action, {"Name": "cast", "Description": "", "GroupType": "AIGC"})
            return httpx.Response(200, json={"Result": {"Id": "group-official"}})
        if action == "CreateAsset":
            _assert_provider_request(request, action, {
                "GroupId": "group-official", "URL": SOURCE, "Name": "portrait", "AssetType": "Image",
            })
            return httpx.Response(200, json={"Result": {"Id": "asset-official"}})
        _assert_provider_request(request, "GetAsset", {"Id": "asset-official"})
        return httpx.Response(200, json={"Result": {
            "Id": "asset-official", "GroupId": "group-official", "ProjectName": ARK_PROJECT, "AssetType": "Image",
            "Status": "Active", "URL": SOURCE,
        }})

    with _client(tmp_path / "scoped.sqlite", upstream) as client:
        alice, alice_project = _project(client, "alice@example.com")
        bob, bob_project = _project(client, "bob@example.com")
        assert client.post(ASSET_GROUP_CREATE, json={"name": "cast"}).status_code == 401
        group_response = client.post(ASSET_GROUP_CREATE, headers=alice, json={"name": " cast "})
        assert group_response.status_code == 201
        group = group_response.json()["group"]
        assert group["id"].startswith("grp_") and "group-official" not in group_response.text
        assert client.get(ASSET_GROUP_LIST, headers=alice).json() == {"groups": [group]}
        assert client.get(ASSET_GROUP_LIST, headers=bob).json() == {"groups": []}
        assert client.get(ASSET_GROUP_LIST, headers=alice, params={"pageNumber": 2}).json() == {"groups": []}

        body = _asset_body(group["id"])
        assert client.post(ASSET_CREATE, headers=bob, json=body).status_code == 404
        assert client.post(ASSET_CREATE, headers=alice, json={**body, "projectName": "foreign"}).status_code == 422
        assert client.post(ASSET_CREATE, headers=alice, json={**body, "url": "https://127.0.0.1/a.jpg"}).status_code == 422
        assert calls == ["CreateAssetGroup"]

        asset_response = client.post(ASSET_CREATE, headers=alice, json=body)
        assert asset_response.status_code == 201
        asset = asset_response.json()["asset"]
        reference = f"asset://{asset['id']}"
        assert asset["status"] == "Processing" and "reference" not in asset
        assert client.get(ASSET_LIST, headers=alice).json() == {"assets": [asset]}
        assert client.get(ASSET_LIST, headers=bob).json() == {"assets": []}
        assert client.get(ASSET_LIST, params={"groupId": group["id"]}, headers=bob).status_code == 404
        assert client.get(ASSET_GET, params={"assetId": asset["id"]}, headers=bob).status_code == 404
        assert client.get(ASSET_GET, params={"assetId": "asset-official"}, headers=alice).status_code == 404
        assert calls == ["CreateAssetGroup", "CreateAsset"]

        with client.app.state.db.connect() as connection:
            with pytest.raises(HTTPException) as processing:
                translate_reference(connection, alice_project, reference, media_type="Image", upstream_project=ARK_PROJECT)
            assert processing.value.detail["code"] == "asset_not_active"
            with pytest.raises(HTTPException) as foreign:
                translate_reference(connection, bob_project, reference, media_type="Image", upstream_project=ARK_PROJECT)
            assert foreign.value.status_code == 404

        ready_response = client.get(ASSET_GET, params={"assetId": asset["id"]}, headers=alice)
        assert ready_response.status_code == 200
        assert ready_response.json()["asset"]["reference"] == reference
        assert ready_response.headers["Cache-Control"] == "no-store"
        assert "asset-official" not in ready_response.text and SOURCE not in ready_response.text
        assert client.get(ASSET_GET, params={"assetId": asset["id"]}, headers=alice).json() == ready_response.json()
        assert calls == ["CreateAssetGroup", "CreateAsset", "GetAsset"]

        with client.app.state.db.connect() as connection:
            assert translate_reference(connection, alice_project, reference, media_type="Image", upstream_project=ARK_PROJECT) == "asset://asset-official"
            with pytest.raises(HTTPException) as wrong_type:
                translate_reference(connection, alice_project, reference, media_type="Video", upstream_project=ARK_PROJECT)
            assert wrong_type.value.detail["code"] == "asset_media_type_mismatch"
            with pytest.raises(HTTPException) as wrong_project:
                translate_reference(connection, alice_project, reference, media_type="Image", upstream_project="different-project")
            assert wrong_project.value.detail["code"] == "asset_provider_project_mismatch"

        duplicate_upstream_group = client.post(ASSET_GROUP_CREATE, headers=bob, json={"name": "cast"})
        assert duplicate_upstream_group.status_code == 502
        assert client.get(ASSET_GROUP_LIST, headers=bob).json() == {"groups": []}


def test_provider_credential_gap_is_closed_and_never_calls_upstream(tmp_path: Path) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("A missing credential or local asset must not call the provider")

    with _client(tmp_path / "unconfigured.sqlite", unexpected, credentials=False) as client:
        customer, _ = _project(client, "alice@example.com")
        failure = client.post(ASSET_GROUP_CREATE, json={"name": "cast"}, headers=customer)
        assert failure.status_code == 503
        assert failure.json()["detail"]["code"] == "asset_provider_unavailable"
        assert client.get(ASSET_GROUP_LIST, headers=customer).json() == {"groups": []}
        assert client.get(ASSET_GET, params={"assetId": "ast_" + "a" * 32}, headers=customer).status_code == 404


def test_invalid_provider_asset_status_cannot_activate_local_mapping(tmp_path: Path) -> None:
    responses = iter((
        httpx.Response(200, json={"Result": {"Id": "group-official"}}),
        httpx.Response(200, json={"Result": {"Id": "asset-official"}}),
        httpx.Response(200, json={"Result": {
            "Id": "asset-different", "GroupId": "group-official", "ProjectName": ARK_PROJECT, "AssetType": "Image", "Status": "Active",
        }}),
        httpx.Response(200, json={"Result": {
            "Id": "asset-official", "GroupId": "group-official", "ProjectName": ARK_PROJECT, "AssetType": "Image", "Status": "Failed",
            "Error": {"Code": "example-provider-error", "Message": SECRET_KEY},
        }}),
    ))
    with _client(tmp_path / "provider.sqlite", lambda _: next(responses)) as client:
        customer, _ = _project(client, "alice@example.com")
        group = client.post(ASSET_GROUP_CREATE, json={"name": "cast"}, headers=customer).json()["group"]
        asset = client.post(ASSET_CREATE, json=_asset_body(group["id"]), headers=customer).json()["asset"]
        bad_status = client.get(ASSET_GET, headers=customer, params={"assetId": asset["id"]})
        assert bad_status.status_code == 502
        assert client.get(ASSET_LIST, headers=customer).json()["assets"][0]["status"] == "Processing"
        failed = client.get(ASSET_GET, headers=customer, params={"assetId": asset["id"]})
        assert failed.status_code == 200
        assert failed.json()["asset"]["status"] == "Failed"
        assert "reference" not in failed.json()["asset"] and SECRET_KEY not in failed.text


def test_create_timeout_does_not_retry_or_persist_an_unconfirmed_group(tmp_path: Path) -> None:
    attempted = []

    def delayed(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.params["Action"])
        raise httpx.ReadTimeout("mock timeout", request=request)

    with _client(tmp_path / "timeout.sqlite", delayed) as client:
        customer, _ = _project(client, "alice@example.com")
        response = client.post(ASSET_GROUP_CREATE, headers=customer, json={"name": "cast"})
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "asset_submission_unknown"
        assert attempted == ["CreateAssetGroup"]
        assert client.get(ASSET_GROUP_LIST, headers=customer).json() == {"groups": []}
