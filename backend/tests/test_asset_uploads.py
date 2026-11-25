from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import json
import logging
from pathlib import Path
from threading import Event
import time
from typing import BinaryIO, Callable
from urllib.parse import urlsplit
import wave

import av
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.upload_registration import redact_source_access_log
from app.upload_storage import LocalUploadStorage, StorageError
from app.uploads import cleanup_uploads


UPLOAD = "/api/asset/upload-file"
REGISTER = "/api/asset/create"
PUBLIC_ORIGIN = "https://relay.example.com"
ARK_PROJECT = "shared-ark-project"


def test_uvicorn_access_logs_omit_source_link_credentials(caplog: pytest.LogCaptureFixture) -> None:
    redact_source_access_log()
    access_log = logging.getLogger("uvicorn.access")
    log_format = '%s - "%s %s HTTP/%s" %d'
    source_path = "/api/asset/source/" + "a" * 43 + ".png"
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access_log.info(log_format, "test-client", "GET", source_path, "1.1", 200)
        access_log.info(log_format, "test-client", "GET", "/health", "1.1", 200)
    assert source_path not in caplog.text
    assert "/health" in caplog.text


class RecordingStorage(LocalUploadStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.puts: list[tuple[str, int, str]] = []
        self.deletes: list[str] = []
        self.fail_put = False
        self.fail_delete = False

    @property
    def objects(self) -> dict[str, bytes]:
        paths = {key: self.path(key) for key, _, _ in self.puts}
        return {key: path.read_bytes() for key, path in paths.items() if path.is_file()}

    def put(self, key: str, file: BinaryIO, *, size: int, content_type: str) -> None:
        self.puts.append((key, size, content_type))
        super().put(key, file, size=size, content_type=content_type)
        if self.fail_put:
            raise StorageError("mock storage details")

    def delete(self, key: str) -> None:
        self.deletes.append(key)
        if self.fail_delete:
            raise StorageError("mock deletion details")
        super().delete(key)


class RecordingArkAssets:
    def __init__(self) -> None:
        self.groups: list[str] = []
        self.submissions: list[dict] = []
        self.failure: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["ProjectName"] == ARK_PROJECT
        action = request.url.params["Action"]
        if action == "CreateAssetGroup":
            group = f"group-official-{len(self.groups) + 1}"
            self.groups.append(group)
            return httpx.Response(200, json={"Result": {"Id": group}})
        if action == "CreateAsset":
            self.submissions.append(body)
            if self.failure == "timeout":
                raise httpx.ReadTimeout("mock provider timeout", request=request)
            if self.failure == "rejected":
                return httpx.Response(400, json={"ResponseMetadata": {"Error": {"Code": "InvalidParameter"}}})
            return httpx.Response(200, json={"Result": {"Id": "asset-official"}})
        assert action == "GetAsset" and body["Id"] == "asset-official"
        submitted = self.submissions[-1]
        return httpx.Response(200, json={"Result": {
            "Id": body["Id"], "GroupId": submitted["GroupId"], "ProjectName": ARK_PROJECT,
            "AssetType": submitted["AssetType"], "Status": "Active",
        }})


def _client(
    path: Path, storage: LocalUploadStorage | None = None, *, raise_server_exceptions: bool = True,
    ark_asset_handler: Callable[[httpx.Request], httpx.Response] | None = None, **options,
) -> TestClient:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Staging a file does not call Ark")

    defaults = {
        "database_path": path, "registration_enabled": True, "cookie_secure": False, "ark_api_key": None,
        "ark_asset_access_key": "test-asset-key" if ark_asset_handler else None,
        "ark_asset_secret_key": "test-asset-secret" if ark_asset_handler else None,
        "ark_asset_project": ARK_PROJECT, "upload_cleanup_seconds": 0, "upload_public_origin": "", "upload_directory": None,
    }
    settings = Settings(**(defaults | options))
    app = create_app(
        settings, ark_transport=httpx.MockTransport(unexpected),
        ark_asset_transport=httpx.MockTransport(ark_asset_handler or unexpected), upload_storage=storage,
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _register(client: TestClient, email: str) -> dict:
    response = client.post("/api/customer/register", json={"email": email, "password": "example-password-123"})
    assert response.status_code == 201
    return response.json()


def _key(client: TestClient, account: dict, name: str) -> tuple[dict[str, str], str]:
    response = client.post("/api/customer/keys", json={"name": name}, headers={"X-CSRF-Token": account["csrfToken"]})
    assert response.status_code == 201
    secret = response.json()["secret"]
    return {"Authorization": f"Bearer {secret}"}, response.json()["key"]["id"]


def _upload(
    client: TestClient,
    auth: dict[str, str],
    content: bytes,
    *,
    filename: str = "sample.png",
    mime: str = "image/png",
    asset_type: str | None = None,
) -> httpx.Response:
    return client.post(
        UPLOAD, headers=auth, files={"file": (filename, content, mime)},
        data={"assetType": asset_type} if asset_type is not None else None,
    )


def _record(client: TestClient, upload_id: str) -> dict:
    with client.app.state.db.connect() as connection:
        record = connection.execute("SELECT * FROM asset_uploads WHERE id = ?", (upload_id,)).fetchone()
    assert record is not None
    return dict(record)


def _group(client: TestClient, auth: dict[str, str]) -> str:
    response = client.post("/api/asset-group/create", headers=auth, json={"name": "Cast"})
    assert response.status_code == 201, response.text
    return response.json()["group"]["id"]


def _staged_id(client: TestClient, auth: dict[str, str], png: bytes) -> str:
    response = _upload(client, auth, png)
    assert response.status_code == 201, response.text
    return response.json()["uploadId"]


def _assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code


def _run_cleanup(client: TestClient) -> None:
    assert client.portal is not None
    client.portal.call(cleanup_uploads, client.app)


@pytest.fixture(scope="module")
def png() -> bytes:
    return _png("#abcabc")


def _png(color: str) -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGB", (512, 512), color=color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def wav() -> bytes:
    return _wav(16000)


def _wav(frame_count: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * frame_count)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def mp4() -> bytes:
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=24)
        stream.width = stream.height = 512
        stream.pix_fmt = "yuv420p"
        with Image.new("RGB", (512, 512), color="#abcabc") as image:
            for _ in range(48):
                container.mux(stream.encode(av.VideoFrame.from_image(image)))
        container.mux(stream.encode())
    return buffer.getvalue()


def test_staging_requires_business_key_and_limits_lookup_to_upload_owner(tmp_path: Path, png: bytes) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "ownership.sqlite", storage) as client:
        alice = _register(client, "alice@example.com")
        alice_key, key_id = _key(client, alice, "main")
        other_key, _ = _key(client, alice, "secondary")

        session_only = _upload(client, {"X-CSRF-Token": alice["csrfToken"]}, png)
        assert session_only.status_code == 401
        assert session_only.headers["WWW-Authenticate"] == "Bearer"
        assert not storage.puts

        bob = _register(client, "bob@example.com")
        bob_key, _ = _key(client, bob, "main")
        first = _upload(client, alice_key, png, filename="../local-private-filename.png")
        assert first.status_code == 201, first.text
        assert first.headers["Cache-Control"] == "no-store"
        body = first.json()
        upload_id = body["uploadId"]
        assert upload_id.startswith("upl_")
        assert body == {
            "uploadId": upload_id,
            "assetType": "Image",
            "contentType": "image/png",
            "sizeBytes": len(png),
            "status": "staged",
            "expiresAt": body["expiresAt"],
        }

        row = _record(client, upload_id)
        assert row["project_id"] == alice["customer"]["projectId"]
        assert row["api_key_id"] == key_id
        assert row["status"] == "staged" and row["expires_at"] > time.time()
        key = row["object_key"]
        assert key.startswith(f"staging/{row['project_id']}/{upload_id}/") and key.endswith(".png")
        assert "local-private-filename" not in key
        assert datetime.fromisoformat(body["expiresAt"]).timestamp() == row["expires_at"]
        assert storage.puts == [(key, len(png), "image/png")]
        assert storage.objects == {key: png}
        assert key not in first.text

        lookup = f"{UPLOAD}/{upload_id}"
        owned = client.get(lookup, headers=alice_key)
        assert owned.status_code == 200 and owned.json() == body
        assert owned.headers["Cache-Control"] == "no-store"
        missing = client.get(f"{UPLOAD}/{'upl_' + '0' * 32}", headers=alice_key)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "upload_not_found"
        for restricted_key in (other_key, bob_key):
            forbidden = client.get(lookup, headers=restricted_key)
            assert forbidden.status_code == 404 and forbidden.json() == missing.json()
        assert client.get(lookup).status_code == 401


@pytest.mark.parametrize(
    ("fixture", "filename", "mime", "asset_type"),
    [
        ("png", "sample.png", "image/png", "Image"),
        ("wav", "sample.wav", "audio/wav", "Audio"),
        ("mp4", "sample.mp4", "video/mp4", "Video"),
    ],
)
def test_stages_real_image_audio_and_video(
    tmp_path: Path, request: pytest.FixtureRequest, fixture: str, filename: str, mime: str, asset_type: str,
) -> None:
    sample = request.getfixturevalue(fixture)
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "media.sqlite", storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        response = _upload(client, auth, sample, filename=filename, mime=mime, asset_type=asset_type)
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["assetType"], body["contentType"], body["sizeBytes"]) == (asset_type, mime, len(sample))
        assert list(storage.objects.values()) == [sample]


def test_invalid_mismatched_and_unrecognized_media_never_reaches_storage(tmp_path: Path, png: bytes) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "invalid.sqlite", storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        cases = (
            (png[:64], {"mime": "image/png"}, 422, "invalid_media"),
            (png, {"asset_type": "Video"}, 415, "media_type_mismatch"),
            (png, {"mime": "image/jpeg"}, 415, "media_type_mismatch"),
            (b"%PDF-1.4\n", {"mime": "application/pdf", "filename": "unsupported.pdf"}, 415, "unsupported_media_type"),
        )
        for content, options, status, code in cases:
            response = _upload(client, auth, content, **options)
            assert response.status_code == status, response.text
            assert response.json()["detail"]["code"] == code
        request_without_multipart = client.post(UPLOAD, headers=auth, json={"url": "https://example.com/test.png"})
        assert request_without_multipart.status_code == 415
        assert request_without_multipart.json()["detail"]["code"] == "multipart_required"
        untrusted_field = client.post(
            UPLOAD, headers=auth, files={"file": ("sample.png", png, "image/png")}, data={"projectId": "foreign"},
        )
        assert untrusted_field.status_code == 422
        assert untrusted_field.json()["detail"]["code"] == "invalid_upload_form"
        incomplete = client.post(
            UPLOAD,
            headers={**auth, "Content-Type": "multipart/form-data; boundary=test"},
            content=b'--test\r\nContent-Disposition: form-data; name="file"; filename="sample.png"\r\n\r\n' + png,
        )
        assert incomplete.status_code == 422 and incomplete.json()["detail"]["code"] == "invalid_upload_form"
        assert not storage.puts and not storage.objects
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM asset_uploads").fetchone()[0] == 0


def test_upload_rejects_oversized_request_and_media(
    tmp_path: Path, png: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "oversized.sqlite", storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        oversized_request = _upload(client, {**auth, "Content-Length": str(201 * 1024 * 1024)}, png)
        oversized_audio = _upload(client, auth, _wav(15 * 1024 * 1024 // 2), filename="sample.wav", mime="audio/wav")
        monkeypatch.setattr("app.uploads.MAX_BODY_BYTES", len(png))
        streamed = client.build_request("POST", UPLOAD, headers=auth, files={"file": ("sample.png", png, "image/png")})
        del streamed.headers["Content-Length"]
        oversized_stream = client.send(streamed)
        for response in (oversized_request, oversized_audio, oversized_stream):
            assert response.status_code == 413, response.text
            assert response.json()["detail"]["code"] == "file_too_large"
        assert not storage.puts


def test_local_storage_never_publishes_a_partial_write_and_cleans_it(tmp_path: Path, png: bytes) -> None:
    class BrokenStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            if self.tell() >= 32:
                raise OSError("private filesystem details")
            return super().read(32)

    storage = LocalUploadStorage(tmp_path / "staged")
    key = f"staging/prj_{'0' * 32}/upl_{'1' * 32}/{'2' * 32}.png"
    path = storage.path(key)
    partial = path.with_name(path.name + ".partial")
    with pytest.raises(StorageError) as failure:
        storage.put(key, BrokenStream(png), size=len(png), content_type="image/png")
    assert "private filesystem details" not in str(failure.value)
    assert not path.exists() and partial.read_bytes() == png[:32]
    storage.delete(key)
    storage.delete(key)
    assert not path.exists() and not partial.exists()

    storage.put(key, io.BytesIO(png), size=len(png), content_type="image/png")
    assert path.read_bytes() == png and not partial.exists()
    with pytest.raises(StorageError):
        storage.path("../profile.png")


def test_idempotent_replay_checks_media_body_and_is_scoped_to_business_key(tmp_path: Path, png: bytes) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "idempotency.sqlite", storage) as client:
        alice = _register(client, "alice@example.com")
        auth, _ = _key(client, alice, "main")
        other_auth, _ = _key(client, alice, "secondary")
        headers = {**auth, "Idempotency-Key": "initial-upload"}

        created = _upload(client, headers, png)
        replayed = _upload(client, headers, png, filename="renamed.png")
        conflict = _upload(client, headers, _png("#fedfed"))
        invalid = _upload(client, {**auth, "Idempotency-Key": "several words"}, png)
        independently_created = _upload(client, {**other_auth, "Idempotency-Key": "initial-upload"}, png)

        assert created.status_code == 201, created.text
        assert replayed.status_code == 200 and replayed.json() == created.json()
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_key_conflict"
        assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "idempotency_key_invalid"
        assert independently_created.status_code == 201, independently_created.text
        assert independently_created.json()["uploadId"] != created.json()["uploadId"]
        assert len(storage.puts) == 2 and len(storage.objects) == 2


def test_concurrent_replay_cannot_start_a_second_upload(tmp_path: Path, png: bytes) -> None:
    entered_storage, resume_storage = Event(), Event()

    class PausingStorage(RecordingStorage):
        def put(self, key: str, file: BinaryIO, *, size: int, content_type: str) -> None:
            entered_storage.set()
            assert resume_storage.wait(10)
            super().put(key, file, size=size, content_type=content_type)

    storage = PausingStorage(tmp_path / "staged")
    with _client(tmp_path / "in-progress.sqlite", storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        headers = {**auth, "Idempotency-Key": "concurrent-upload"}
        with ThreadPoolExecutor(max_workers=1) as worker:
            first = worker.submit(_upload, client, headers, png)
            try:
                assert entered_storage.wait(10)
                with client.app.state.db.connect() as connection:
                    record = connection.execute("SELECT status FROM asset_uploads").fetchone()
                assert record["status"] == "uploading"
                replay = _upload(client, headers, png)
                assert replay.status_code == 409 and replay.json()["detail"]["code"] == "upload_in_progress"
                _run_cleanup(client)
                assert not storage.deletes
            finally:
                resume_storage.set()
            assert first.result(timeout=10).status_code == 201
        assert len(storage.puts) == 1


def test_failed_put_survives_restart_and_retries_after_confirmed_cleanup(tmp_path: Path, png: bytes) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    storage.fail_put = True
    path = tmp_path / "retry.sqlite"
    with _client(path, storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        headers = {**auth, "Idempotency-Key": "retryable-upload"}
        failed = _upload(client, headers, png)
        assert failed.status_code == 502
        detail = failed.json()["detail"]
        assert detail["code"] == "upload_storage_failed"
        assert "mock storage details" not in failed.text
        upload_id = detail["uploadId"]
        first_attempt = _record(client, upload_id)
        assert first_attempt["status"] == "cleanup_pending"
        assert first_attempt["cleanup_after"] > time.time()
        key = first_attempt["object_key"]
        assert storage.objects[key] == png
        blocked = _upload(client, headers, png)
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "upload_in_progress"
        assert len(storage.puts) == 1
        _run_cleanup(client)
        assert not storage.deletes

    with _client(path, storage) as client:
        lookup = f"{UPLOAD}/{upload_id}"
        assert client.get(lookup, headers=auth).json()["status"] == "cleanup_pending"
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET cleanup_after = 0 WHERE id = ?", (upload_id,))

        storage.fail_delete = True
        _run_cleanup(client)
        assert storage.deletes == [key] and storage.objects[key] == png
        after_failure = _record(client, upload_id)
        assert after_failure["status"] == "cleanup_pending" and after_failure["cleanup_after"] > time.time()

        storage.fail_delete = False
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET cleanup_after = 0 WHERE id = ?", (upload_id,))
        _run_cleanup(client)
        _run_cleanup(client)
        assert storage.deletes == [key, key] and key not in storage.objects
        assert client.get(lookup, headers=auth).json()["status"] == "failed"

        storage.fail_put = False
        retried = _upload(client, headers, png)
        assert retried.status_code == 201 and retried.json()["uploadId"] == upload_id
        second_attempt = _record(client, upload_id)
        assert second_attempt["status"] == "staged" and second_attempt["object_key"] != key
        assert storage.objects == {second_attempt["object_key"]: png}


def test_interrupted_put_recovers_after_lease_and_reuses_id_with_new_object_key(tmp_path: Path, png: bytes) -> None:
    class InterruptedStorage(RecordingStorage):
        interrupt = True

        def put(self, key: str, file: BinaryIO, *, size: int, content_type: str) -> None:
            super().put(key, file, size=size, content_type=content_type)
            if self.interrupt:
                raise RuntimeError("Application interrupted before recording the PUT outcome")

    storage = InterruptedStorage(tmp_path / "staged")
    path = tmp_path / "interrupted.sqlite"
    with _client(path, storage, raise_server_exceptions=False) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        headers = {**auth, "Idempotency-Key": "interrupted-upload"}
        assert _upload(client, headers, png).status_code == 500
        with client.app.state.db.connect() as connection:
            reservation = connection.execute("SELECT id, status, cleanup_after FROM asset_uploads").fetchone()
        assert reservation["status"] == "uploading" and reservation["cleanup_after"] > time.time()
        upload_id = reservation["id"]
        original_key = next(iter(storage.objects))

    with _client(path, storage) as client:
        lookup = f"{UPLOAD}/{upload_id}"
        assert client.get(lookup, headers=auth).json()["status"] == "uploading"
        _run_cleanup(client)
        assert not storage.deletes
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET cleanup_after = 0 WHERE id = ?", (upload_id,))
        _run_cleanup(client)
        _run_cleanup(client)
        assert storage.deletes == [original_key] and not storage.objects
        assert client.get(lookup, headers=auth).json()["status"] == "failed"

        storage.interrupt = False
        retried = _upload(client, headers, png)
        assert retried.status_code == 201 and retried.json()["uploadId"] == upload_id
        assert original_key not in storage.objects and list(storage.objects.values()) == [png]


def test_expiration_cleans_staged_file_once_and_does_not_recreate_on_replay(tmp_path: Path, png: bytes) -> None:
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "expiry.sqlite", storage) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        headers = {**auth, "Idempotency-Key": "expiring-upload"}
        first = _upload(client, headers, png)
        assert first.status_code == 201, first.text
        upload_id = first.json()["uploadId"]
        row = _record(client, upload_id)
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET expires_at = 0, cleanup_after = 0 WHERE id = ?", (upload_id,))
        assert client.get(f"{UPLOAD}/{upload_id}", headers=auth).json()["status"] == "cleanup_pending"
        _run_cleanup(client)
        _run_cleanup(client)

        assert storage.deletes == [row["object_key"]] and not storage.objects
        assert client.get(f"{UPLOAD}/{upload_id}", headers=auth).json()["status"] == "expired"
        replayed = _upload(client, headers, png)
        assert replayed.status_code == 409 and replayed.json()["detail"]["code"] == "upload_expired"
        assert len(storage.puts) == 1


def test_registering_local_upload_scopes_owner_and_serves_expiring_ark_source(tmp_path: Path, png: bytes) -> None:
    provider = RecordingArkAssets()
    path = tmp_path / "registration.sqlite"
    with _client(path, ark_asset_handler=provider, upload_public_origin=PUBLIC_ORIGIN, upload_ttl_seconds=60) as client:
        alice = _register(client, "alice@example.com")
        auth, _ = _key(client, alice, "main")
        other_key, _ = _key(client, alice, "secondary")
        bob_key, _ = _key(client, _register(client, "bob@example.com"), "main")
        group = _group(client, auth)
        bob_group = _group(client, bob_key)
        upload_id = _staged_id(client, auth, png)
        base = {"groupId": group, "name": "Portrait", "assetType": "Image"}
        registration = {**base, "uploadId": upload_id}
        for body in (base, {**registration, "url": "https://example.com/image.png"},
                     {k: v for k, v in registration.items() if k != "assetType"}):
            _assert_error(client.post(REGISTER, headers=auth, json=body), 422, "invalid_request")
        _assert_error(client.post(REGISTER, headers=other_key, json=registration), 404, "upload_not_found")
        _assert_error(client.post(REGISTER, headers=bob_key, json={**registration, "groupId": bob_group}), 404, "upload_not_found")
        _assert_error(client.post(REGISTER, headers=auth, json={**registration, "assetType": "Video"}), 422, "asset_media_type_mismatch")
        assert provider.submissions == []

        created = client.post(REGISTER, headers={**auth, "Host": "untrusted.example.com"}, json=registration)
        assert created.status_code == 201, created.text
        asset = created.json()["asset"]
        assert asset["id"].startswith("ast_") and asset["status"] == "Processing"
        [submission] = provider.submissions
        url = submission["URL"]
        source = urlsplit(url).path
        assert url == f"{PUBLIC_ORIGIN}{source}" and source.startswith("/api/asset/source/") and source.endswith(".png")
        assert submission == {
            "GroupId": provider.groups[0], "URL": url, "Name": "Portrait", "AssetType": "Image", "ProjectName": ARK_PROJECT,
        }
        assert source not in created.text and "asset-official" not in created.text
        with client.app.state.db.connect() as connection:
            fetch_record = connection.execute(
                "SELECT fetch_token_hash, fetch_expires_at FROM asset_upload_registrations WHERE upload_id = ?", (upload_id,),
            ).fetchone()
        assert fetch_record["fetch_token_hash"] == hashlib.sha256(source.rsplit("/", 1)[1].encode()).hexdigest()
        staged = _record(client, upload_id)
        assert staged["expires_at"] < fetch_record["fetch_expires_at"] == staged["cleanup_after"]
        staged_path = client.app.state.upload_storage.path(staged["object_key"])

        repeat = client.post(REGISTER, headers=auth, json=registration)
        assert repeat.status_code == 200 and repeat.json() == created.json()
        _assert_error(client.post(REGISTER, headers=auth, json={**registration, "name": "Different name"}), 409, "upload_registration_conflict")
        _assert_error(client.post(REGISTER, headers=auth, json={**base, "url": url}), 422, "upload_source_requires_id")
        assert provider.submissions == [submission]
        assert client.get("/api/asset/list", headers=auth).json() == {"assets": [asset]}
        assert client.get("/api/asset/list", headers=bob_key).json() == {"assets": []}
        active = client.get("/api/asset/get", headers=auth, params={"assetId": asset["id"]}).json()["asset"]
        assert active["status"] == "Active" and active["reference"] == f"asset://{asset['id']}"

        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET expires_at = 0 WHERE id = ?", (upload_id,))
        _run_cleanup(client)
        assert staged_path.is_file()
        client.cookies.clear()
        download = client.get(source)
        assert download.status_code == 200 and download.content == png
        assert download.headers["Content-Type"] == "image/png" and download.headers["Cache-Control"] == "no-store"
        head = client.head(source)
        assert head.status_code == 200 and head.content == b""
        assert head.headers["Content-Type"] == "image/png" and head.headers["Content-Length"] == str(len(png))
        byte_range = client.get(source, headers={"Range": "bytes=2-5"})
        assert byte_range.status_code == 206 and byte_range.content == png[2:6]
        assert byte_range.headers["Content-Range"] == f"bytes 2-5/{len(png)}"
        assert client.get(source.removesuffix(".png") + ".jpg").status_code == 404

        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_upload_registrations SET fetch_expires_at = 0 WHERE upload_id = ?", (upload_id,))
            connection.execute("UPDATE asset_uploads SET cleanup_after = 0 WHERE id = ?", (upload_id,))
        assert client.get(source).status_code == 404
        assert client.head(source).status_code == 404
        _run_cleanup(client)
        assert not staged_path.exists()
        assert client.get("/api/asset/get", headers=auth, params={"assetId": asset["id"]}).json()["asset"] == active


def test_unconfirmed_registration_keeps_source_and_prevents_second_submission(tmp_path: Path, png: bytes) -> None:
    provider = RecordingArkAssets()
    provider.failure = "timeout"
    path = tmp_path / "pending.sqlite"
    with _client(path, ark_asset_handler=provider, upload_public_origin=PUBLIC_ORIGIN) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        body = {"groupId": _group(client, auth), "name": "Portrait", "assetType": "Image", "uploadId": _staged_id(client, auth, png)}
        failed = client.post(REGISTER, headers=auth, json=body)
        _assert_error(failed, 502, "asset_submission_unknown")
        [submission] = provider.submissions
        source = urlsplit(submission["URL"]).path
        assert source not in failed.text

    with _client(path, ark_asset_handler=provider, upload_public_origin=PUBLIC_ORIGIN) as client:
        _assert_error(client.post(REGISTER, headers=auth, json=body), 409, "upload_registration_pending")
        assert provider.submissions == [submission]
        assert client.get(source).content == png
        assert client.get("/api/asset/list", headers=auth).json() == {"assets": []}


def test_rejected_registration_releases_source_and_can_retry(tmp_path: Path, png: bytes) -> None:
    provider = RecordingArkAssets()
    provider.failure = "rejected"
    with _client(tmp_path / "rejection.sqlite", ark_asset_handler=provider, upload_public_origin=PUBLIC_ORIGIN) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        body = {"groupId": _group(client, auth), "name": "Portrait", "assetType": "Image", "uploadId": _staged_id(client, auth, png)}
        _assert_error(client.post(REGISTER, headers=auth, json=body), 502, "asset_provider_rejected")
        [first] = provider.submissions
        assert client.get(urlsplit(first["URL"]).path).status_code == 404

        provider.failure = None
        retried = client.post(REGISTER, headers=auth, json=body)
        assert retried.status_code == 201, retried.text
        assert len(provider.submissions) == 2 and first["URL"] != provider.submissions[-1]["URL"]
        assert client.get(urlsplit(provider.submissions[-1]["URL"]).path).content == png


def test_local_registration_requires_configured_public_origin(tmp_path: Path, png: bytes) -> None:
    provider = RecordingArkAssets()
    with _client(tmp_path / "unconfigured.sqlite", ark_asset_handler=provider) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        body = {"groupId": _group(client, auth), "name": "Portrait", "assetType": "Image", "uploadId": _staged_id(client, auth, png)}
        _assert_error(client.post(REGISTER, headers=auth, json=body), 503, "upload_registration_unavailable")
        assert not provider.submissions


def test_legacy_tos_records_are_preserved_without_accessing_local_objects(tmp_path: Path, png: bytes) -> None:
    provider = RecordingArkAssets()
    storage = RecordingStorage(tmp_path / "staged")
    with _client(tmp_path / "legacy.sqlite", storage, ark_asset_handler=provider, upload_public_origin=PUBLIC_ORIGIN) as client:
        auth, _ = _key(client, _register(client, "alice@example.com"), "main")
        headers = {**auth, "Idempotency-Key": "old-upload"}
        upload_id = _staged_id(client, headers, png)
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_uploads SET bucket = 'old-tos', cleanup_after = 0 WHERE id = ?", (upload_id,))
        legacy = _record(client, upload_id)

        _assert_error(client.get(f"{UPLOAD}/{upload_id}", headers=auth), 409, "upload_storage_changed")
        _assert_error(_upload(client, headers, png), 409, "upload_storage_changed")
        body = {"groupId": _group(client, auth), "name": "Portrait", "assetType": "Image", "uploadId": upload_id}
        _assert_error(client.post(REGISTER, headers=auth, json=body), 409, "upload_storage_changed")
        _run_cleanup(client)
        assert _record(client, upload_id) == legacy
        assert not provider.submissions and not storage.deletes
        assert _staged_id(client, {**auth, "Idempotency-Key": "new-upload"}, png) != upload_id
