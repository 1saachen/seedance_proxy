import hashlib
import io
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from app.assets import ARK_ASSET_HOST, translate_reference
from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access


ASSET_GROUP_CREATE = "/api/asset-group/create"
ASSET_GROUP_LIST = "/api/asset-group/list"
ASSET_GROUP_GET = "/api/asset-group/get"
ASSET_GROUP_UPDATE = "/api/asset-group/update"
ASSET_GROUP_DELETE = "/api/asset-group/delete"
ASSET_CREATE = "/api/asset/create"
ASSET_GET = "/api/asset/get"
ASSET_LIST = "/api/asset/list"
ASSET_UPDATE = "/api/asset/update"
ASSET_DELETE = "/api/asset/delete"
LIVENESS_START = "/api/asset-group/liveness/start"
LIVENESS_COMPLETE = "/api/asset-group/liveness/complete"
ARK_PROJECT = "shared-ark-project"
ACCESS_KEY = "example-asset-access-key"
SECRET_KEY = "example-asset-secret-key"
SOURCE = "https://media.example.com/reference.png?token=example-private-token"


def _client(path: Path, handler, *, credentials: bool = True, upload_origin: str = "") -> TestClient:
    settings = Settings(
        database_path=path,
        registration_enabled=True,
        cookie_secure=False,
        ark_api_key=None,
        ark_asset_access_key=SecretStr(ACCESS_KEY) if credentials else None,
        ark_asset_secret_key=SecretStr(SECRET_KEY) if credentials else None,
        ark_asset_project=ARK_PROJECT,
        upload_public_origin=upload_origin,
    )
    return TestClient(create_app(settings, ark_asset_transport=httpx.MockTransport(handler)))


def _project(client: TestClient, email: str) -> tuple[dict, str]:
    signup = client.post("/api/customer/register", json={"email": email, "password": "example-password-123"})
    assert signup.status_code == 201
    account = signup.json()
    with client.app.state.db.connect() as connection:
        set_seedance_access(connection, account["customer"]["projectId"], True)
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


def test_manage_owned_assets_and_groups_without_exposing_provider_ids(tmp_path: Path) -> None:
    calls = []
    responses = iter([
        ("CreateAssetGroup", {"Name": "Cast", "Description": "Before", "GroupType": "AIGC"}, {"Id": "group-owned"}),
        ("GetAssetGroup", {"Id": "group-owned"}, {
            "Id": "group-owned", "ProjectName": ARK_PROJECT, "GroupType": "AIGC", "Name": "Cast", "Description": "Before",
        }),
        ("UpdateAssetGroup", {"Id": "group-owned", "Name": "Cast Updated", "Description": ""}, {"Id": "group-owned"}),
        ("CreateAsset", {"GroupId": "group-owned", "URL": SOURCE, "Name": "portrait", "AssetType": "Image"}, {"Id": "asset-image"}),
        ("CreateAsset", {"GroupId": "group-owned", "URL": SOURCE, "Name": "voice", "AssetType": "Audio"}, {"Id": "asset-audio"}),
        ("GetAsset", {"Id": "asset-image"}, {
            "Id": "asset-image", "GroupId": "group-owned", "ProjectName": ARK_PROJECT, "AssetType": "Image", "Status": "Active",
        }),
        ("UpdateAsset", {"Id": "asset-image", "Name": "Portrait_%"}, {"Id": "asset-image"}),
        ("DeleteAsset", {"Id": "asset-audio"}, {}),
        ("DeleteAssetGroup", {"Id": "group-owned"}, {}),
    ])

    def provider(request: httpx.Request) -> httpx.Response:
        action, body, result = next(responses)
        _assert_provider_request(request, action, body)
        calls.append(action)
        return httpx.Response(200, json={"Result": result})

    with _client(tmp_path / "manage.sqlite", provider) as client:
        alice, project = _project(client, "alice@example.com")
        bob, _ = _project(client, "bob@example.com")
        group = client.post(ASSET_GROUP_CREATE, headers=alice, json={"name": "Cast", "description": "Before"}).json()["group"]
        group_ids = {"groupId": group["id"]}
        assert client.get(ASSET_GROUP_GET, headers=bob, params=group_ids).status_code == 404
        assert client.post(ASSET_GROUP_UPDATE, headers=bob, json={**group_ids, "name": "Foreign"}).status_code == 404
        assert client.post(ASSET_GROUP_DELETE, headers=bob, json=group_ids).status_code == 404
        assert client.post(ASSET_GROUP_UPDATE, headers=alice, json=group_ids).status_code == 422
        assert client.get(ASSET_GROUP_GET, headers=alice, params=group_ids).json()["group"] == group
        changed = client.post(ASSET_GROUP_UPDATE, headers=alice, json={**group_ids, "name": "Cast Updated", "description": ""})
        assert changed.status_code == 200 and changed.json()["group"] == {**group, "name": "Cast Updated", "description": ""}
        filters = [*group_ids.items(), ("groupId", "grp_missing"), ("name", "UpDaTeD"), ("sortOrder", "asc")]
        assert client.get(ASSET_GROUP_LIST, headers=alice, params=filters).json()["groups"] == [changed.json()["group"]]
        assert client.get(ASSET_GROUP_LIST, headers=bob, params=filters).json()["groups"] == []

        image = client.post(ASSET_CREATE, headers=alice, json=_asset_body(group["id"])).json()["asset"]
        audio = client.post(ASSET_CREATE, headers=alice, json={**_asset_body(group["id"]), "assetType": "Audio", "name": "voice"}).json()["asset"]
        image_ids = {"assetId": image["id"]}
        audio_ids = {"assetId": audio["id"]}
        assert client.post(ASSET_UPDATE, headers=bob, json={**image_ids, "name": "Foreign"}).status_code == 404
        assert client.post(ASSET_DELETE, headers=bob, json=image_ids).status_code == 404
        got = client.get(ASSET_GET, headers=alice, params=image_ids).json()["asset"]
        changed = client.post(ASSET_UPDATE, headers=alice, json={**image_ids, "name": "Portrait_%"})
        assert changed.status_code == 200 and changed.json()["asset"] == {**got, "name": "Portrait_%"}
        assert "asset-image" not in changed.text and SOURCE not in changed.text
        assert client.get(ASSET_LIST, headers=alice, params=[
            *group_ids.items(), ("name", "_%"), ("assetType", "Image"), ("status", "Active"), ("status", "Failed"),
        ]).json()["assets"] == [changed.json()["asset"]]
        assert client.get(ASSET_LIST, headers=alice, params={"assetType": "Audio", "status": "Processing"}).json()["assets"] == [audio]
        assert client.post(ASSET_DELETE, headers=alice, json=audio_ids).status_code == 204
        assert client.post(ASSET_DELETE, headers=alice, json=audio_ids).status_code == 204
        assert client.get(ASSET_GET, headers=alice, params=audio_ids).status_code == 404
        assert client.post(ASSET_GROUP_DELETE, headers=alice, json=group_ids).status_code == 204
        assert client.post(ASSET_GROUP_DELETE, headers=alice, json=group_ids).status_code == 204
        assert client.post(ASSET_DELETE, headers=alice, json=image_ids).status_code == 204
        assert client.get(ASSET_GROUP_GET, headers=alice, params=group_ids).status_code == 404
        assert client.get(ASSET_GET, headers=alice, params=image_ids).status_code == 404
        assert client.get(ASSET_LIST, headers=alice).json() == {"assets": []}
        assert client.get(ASSET_GROUP_LIST, headers=alice).json() == {"groups": []}
        assert client.post(ASSET_CREATE, headers=alice, json=_asset_body(group["id"])).status_code == 404
        assert client.post(ASSET_GROUP_DELETE, headers=bob, json=group_ids).status_code == 404
        with client.app.state.db.connect() as connection:
            with pytest.raises(HTTPException) as removed:
                translate_reference(connection, project, got["reference"], media_type="Image", upstream_project=ARK_PROJECT)
            assert removed.value.detail["code"] == "asset_not_found"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert calls.count("DeleteAsset") == 1 and calls.count("DeleteAssetGroup") == 1
        assert list(responses) == []


def test_group_delete_retries_unknown_result_and_retains_upload_records(tmp_path: Path) -> None:
    calls = []
    source_path = None

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal source_path
        action = request.url.params["Action"]
        calls.append(action)
        if action == "CreateAssetGroup":
            return httpx.Response(200, json={"Result": {"Id": "group-owned"}})
        if action == "CreateAsset":
            source_path = httpx.URL(json.loads(request.content)["URL"]).path
            return httpx.Response(200, json={"Result": {"Id": "asset-image"}})
        _assert_provider_request(request, "DeleteAssetGroup", {"Id": "group-owned"})
        if calls.count("DeleteAssetGroup") == 1:
            return httpx.Response(403, json={"ResponseMetadata": {"Error": {"Code": "AccessDenied"}}})
        if calls.count("DeleteAssetGroup") == 2:
            raise httpx.ReadTimeout("provider response lost", request=request)
        return httpx.Response(404, json={"ResponseMetadata": {"Action": "DeleteAssetGroup", "Error": {"Code": "NotFound"}}})

    image = io.BytesIO()
    Image.new("RGB", (32, 32), "purple").save(image, format="PNG")
    with _client(tmp_path / "manage-uploads.sqlite", provider, upload_origin="https://relay.example.com") as client:
        auth, _ = _project(client, "alice@example.com")
        group_id = client.post(ASSET_GROUP_CREATE, headers=auth, json={"name": "Cast"}).json()["group"]["id"]
        staged = client.post("/api/asset/upload-file", headers=auth, files={"file": ("upload.png", image.getvalue(), "image/png")})
        assert staged.status_code == 201
        upload_id = staged.json()["uploadId"]
        registration_body = {"groupId": group_id, "name": "portrait", "assetType": "Image", "uploadId": upload_id}
        registered = client.post(ASSET_CREATE, headers=auth, json=registration_body)
        assert registered.status_code == 201
        asset_id = registered.json()["asset"]["id"]
        assert source_path and client.get(source_path).status_code == 200
        deletion_body = {"groupId": group_id}
        rejected = client.post(ASSET_GROUP_DELETE, headers=auth, json=deletion_body)
        assert rejected.status_code == 502 and rejected.json()["detail"]["code"] == "asset_provider_rejected"
        assert len(client.get(ASSET_GROUP_LIST, headers=auth).json()["groups"]) == 1
        unknown = client.post(ASSET_GROUP_DELETE, headers=auth, json=deletion_body)
        assert unknown.status_code == 502 and unknown.json()["detail"]["code"] == "asset_mutation_unknown"
        pending = client.get(ASSET_GET, headers=auth, params={"assetId": asset_id})
        assert pending.status_code == 409 and pending.json()["detail"]["code"] == "asset_deletion_pending"
        assert client.get(source_path).status_code == 200
        assert client.post(ASSET_GROUP_DELETE, headers=auth, json=deletion_body).status_code == 204
        assert client.post(ASSET_GROUP_DELETE, headers=auth, json=deletion_body).status_code == 204
        assert client.get(source_path).status_code == 404
        assert client.post(ASSET_CREATE, headers=auth, json=registration_body).status_code == 404
        assert client.get(f"/api/asset/upload-file/{upload_id}", headers=auth).status_code == 200
        with client.app.state.db.connect() as connection:
            registration = connection.execute("SELECT * FROM asset_upload_registrations WHERE upload_id = ?", (upload_id,)).fetchone()
            assert registration["group_id"] == group_id and registration["asset_id"] == asset_id
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert calls.count("DeleteAssetGroup") == 3


def test_liveness_maps_only_a_provider_verified_session_to_the_creating_project_and_key(tmp_path: Path) -> None:
    token = "synthetic-byted-token-first"
    link = "https://ark.volcengine.com/region:cn-beijing/mobile/livenees-face-manage/authorization?pl=synthetic"
    callback = "https://consumer.example.com/verified"
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        action = request.url.params["Action"]
        calls.append(action)
        if action == "CreateVisualValidateSession":
            _assert_provider_request(request, action, {"CallbackURL": callback})
            return httpx.Response(200, json={"Result": {"H5Link": link, "BytedToken": token, "CallbackURL": callback}})
        if action == "GetVisualValidateResult":
            _assert_provider_request(request, action, {"BytedToken": token})
            result = {"GroupId": "group-verified"} if calls.count(action) == 2 else {}
            return httpx.Response(200, json={"Result": result})
        _assert_provider_request(request, "CreateAsset", {
            "GroupId": "group-verified", "URL": SOURCE, "Name": "portrait", "AssetType": "Image",
        })
        return httpx.Response(200, json={"Result": {"Id": "asset-verified"}})

    with _client(tmp_path / "liveness.sqlite", provider) as client:
        alice, project_id = _project(client, "alice@example.com")
        csrf = client.get("/api/customer/me").json()["csrfToken"]
        key = client.post("/api/customer/keys", headers={"X-CSRF-Token": csrf}, json={"name": "sibling"})
        sibling = {"Authorization": f"Bearer {key.json()['secret']}"}
        bob, _ = _project(client, "bob@example.com")
        assert client.post(LIVENESS_START, json={"callbackUrl": callback}).status_code == 401
        assert client.post(LIVENESS_START, headers=alice, json={"callbackUrl": "https://localhost/verify"}).status_code == 422
        assert client.post(ASSET_GROUP_CREATE, headers=alice, json={"name": "cast", "groupType": "LivenessFace"}).status_code == 422
        started = client.post(LIVENESS_START, headers=alice, json={"callbackUrl": callback})
        assert started.status_code == 201 and started.headers["Cache-Control"] == "no-store"
        assert started.json()["authorizationUrl"] == link and token not in started.text
        session_id = started.json()["sessionId"]
        body = {"sessionId": session_id, "bytedToken": token}
        assert client.post(LIVENESS_COMPLETE, headers=bob, json=body).status_code == 404
        assert client.post(LIVENESS_COMPLETE, headers=sibling, json=body).status_code == 404
        assert client.post(LIVENESS_COMPLETE, headers=alice, json={**body, "bytedToken": "wrong-token"}).status_code == 404
        assert client.post(LIVENESS_COMPLETE, headers=alice, json={**body, "resultCode": 10000}).status_code == 422
        assert calls == ["CreateVisualValidateSession"]
        unconfirmed = client.post(LIVENESS_COMPLETE, headers=alice, json=body)
        assert unconfirmed.status_code == 409 and unconfirmed.json()["detail"]["code"] == "asset_liveness_unconfirmed"
        confirmed = client.post(LIVENESS_COMPLETE, headers=alice, json=body)
        assert confirmed.status_code == 200 and "group-verified" not in confirmed.text
        group = confirmed.json()["group"]
        assert group["groupType"] == "LivenessFace" and group["id"].startswith("grp_")
        assert client.post(LIVENESS_COMPLETE, headers=alice, json=body).json() == confirmed.json()
        assert client.get(ASSET_GROUP_LIST, headers=alice, params={"groupType": "LivenessFace"}).json() == {"groups": [group]}
        assert client.get(ASSET_GROUP_LIST, headers=alice, params={"groupType": "AIGC"}).json() == {"groups": []}
        assert client.post(ASSET_CREATE, headers=bob, json=_asset_body(group["id"])).status_code == 404
        asset = client.post(ASSET_CREATE, headers=alice, json=_asset_body(group["id"]))
        assert asset.status_code == 201
        assert client.get(ASSET_LIST, headers=alice, params={"groupType": "LivenessFace"}).json() == {"assets": [asset.json()["asset"]]}
        with client.app.state.db.connect() as connection:
            stored = connection.execute("SELECT * FROM asset_liveness_sessions WHERE id = ?", (session_id,)).fetchone()
            assert stored["project_id"] == project_id and stored["group_id"] == group["id"]
            assert stored["token_hash"] == hashlib.sha256(token.encode("ascii")).hexdigest()
            assert token not in tuple(stored) and link not in tuple(stored) and callback not in tuple(stored)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert calls == ["CreateVisualValidateSession", "GetVisualValidateResult", "GetVisualValidateResult", "CreateAsset"]


def test_liveness_rejects_ambiguous_callback_non_provider_links_and_expired_tokens(tmp_path: Path) -> None:
    calls = []
    token = "synthetic-expiring-token"
    callback = "https://consumer.example.com/verified"
    provider_path = "/region:cn-beijing/mobile/livenees-face-manage/authorization?pl=synthetic"

    def provider(request: httpx.Request) -> httpx.Response:
        _assert_provider_request(request, "CreateVisualValidateSession", {"CallbackURL": callback})
        calls.append(request.url.params["Action"])
        host = "ark.volcengine.com.phishing.example.com" if len(calls) == 1 else "ark.volcengine.com"
        return httpx.Response(200, json={"Result": {"H5Link": f"https://{host}{provider_path}", "BytedToken": token}})

    with _client(tmp_path / "liveness-expired.sqlite", provider) as client:
        auth, _ = _project(client, "alice@example.com")
        assert client.post(LIVENESS_START, headers=auth, json={"callbackUrl": f"{callback}?bytedToken=spoof"}).status_code == 422
        suspect = client.post(LIVENESS_START, headers=auth, json={"callbackUrl": callback})
        assert suspect.status_code == 502 and "phishing.example.com" not in suspect.text
        started = client.post(LIVENESS_START, headers=auth, json={"callbackUrl": callback})
        assert started.status_code == 201
        session_id = started.json()["sessionId"]
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM asset_liveness_sessions").fetchone()[0] == 1
            connection.execute("UPDATE asset_liveness_sessions SET expires_at = 0 WHERE id = ?", (session_id,))
        expired = client.post(LIVENESS_COMPLETE, headers=auth, json={"sessionId": session_id, "bytedToken": token})
        assert expired.status_code == 409 and expired.json()["detail"]["code"] == "asset_liveness_expired"
        assert calls == ["CreateVisualValidateSession", "CreateVisualValidateSession"]
