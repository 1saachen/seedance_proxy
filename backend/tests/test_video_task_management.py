import asyncio
from datetime import UTC, datetime
import hashlib
import hmac
import json
import socket

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access
from app.model_api import TASKS_PATH, poll_unfinished
from app import video_callbacks


PAYLOAD = {"model": "doubao-seedance-2.5", "content": [{"type": "text", "text": "Morning on a quiet island"}]}


def _test_client(tmp_path, provider, callback_poll_seconds=0, **settings):
    return TestClient(create_app(Settings(
        database_path=tmp_path / "task-management.sqlite", registration_enabled=True, cookie_secure=False,
        ark_api_key="example-ark-key", video_poll_seconds=0, video_callback_poll_seconds=callback_poll_seconds,
        upload_cleanup_seconds=0, **settings,
    ), ark_transport=httpx.MockTransport(provider)))


def _account(client, email):
    client.cookies.clear()
    response = client.post("/api/customer/register", json={"email": email, "password": "example-password"})
    assert response.status_code == 201
    account = response.json()
    with client.app.state.db.connect() as connection:
        set_seedance_access(connection, account["customer"]["projectId"], True)
    return account["csrfToken"]


def _new_key(client, csrf):
    response = client.post("/api/customer/keys", headers={"X-CSRF-Token": csrf}, json={"name": "test key"})
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["secret"]}


def _submit(client, headers, **changes):
    response = client.post(TASKS_PATH, headers=headers, json={**PAYLOAD, **changes})
    assert response.status_code == 200, response.text
    return response.json()["id"]


class ArkTasks:
    def __init__(self):
        self.states = {}
        self.calls = []
        self.cancel_result = "cancelled"
        self.drop_delete_response = False

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert "callback_url" not in json.loads(request.content)
            task_id = f"provider-task-{len(self.states) + 1}"
            self.states[task_id] = "queued"
            return httpx.Response(200, json={"id": task_id})
        task_id = request.url.path.split("/")[-1]
        if request.method == "DELETE":
            assert self.states[task_id] == "queued"
            self.states[task_id] = self.cancel_result
            if self.drop_delete_response:
                raise httpx.ReadTimeout("synthetic lost DELETE", request=request)
            return httpx.Response(200, json={})
        state = self.states[task_id]
        if state == "missing":
            return httpx.Response(404, json={"error": "test fixture"})
        return httpx.Response(200, json={
            "id": task_id, "status": state, "updated_at": int(datetime.now(UTC).timestamp()),
            "content": {"video_url": "https://media.example.com/fixture.mp4"}, "usage": {"completion_tokens": 212},
        })


def test_task_list_filters_isolate_keys_and_use_the_local_snapshot(tmp_path):
    provider = ArkTasks()
    with _test_client(tmp_path, provider) as client:
        csrf = _account(client, "alice@example.com")
        owner, same_project = _new_key(client, csrf), _new_key(client, csrf)
        first = _submit(client, owner)
        second = _submit(client, owner, model="doubao-seedance-2.0")
        hidden_from_owner = _submit(client, same_project)
        outsider = _new_key(client, _account(client, "bob@example.com"))
        other_project = _submit(client, outsider)
        provider.states["provider-task-1"] = "succeeded"
        assert client.get(f"{TASKS_PATH}/{first}", headers=owner).json()["status"] == "succeeded"
        upstream_calls = list(provider.calls)

        default = client.get(TASKS_PATH, headers=owner, params={"page_size": 1}).json()
        assert default["total"] == 2 and len(default["items"]) == 1
        next_page = client.get(TASKS_PATH, headers=owner, params={"page_size": 1, "page_num": 2}).json()
        assert {default["items"][0]["id"], next_page["items"][0]["id"]} == {first, second}
        response = client.get(TASKS_PATH, headers=owner, params=[
            ("filter.task_ids", first), ("filter.task_ids", other_project), ("filter.task_ids", hidden_from_owner),
            ("filter.model", PAYLOAD["model"]), ("filter.status", "succeeded"), ("filter.service_tier", "default"),
        ])
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["id"] == first
        assert "provider-task" not in response.text
        assert client.get(TASKS_PATH, headers=owner, params={"filter.service_tier": "flex"}).json() == {"total": 0, "items": []}
        assert client.get(TASKS_PATH, headers=owner, params={"filter.task_ids": "invalid"}).status_code == 422
        assert client.get(TASKS_PATH, headers=owner, params={"page_size": 501}).status_code == 422
        assert client.get(TASKS_PATH).status_code == 401
        assert provider.calls == upstream_calls


def test_cancel_confirms_upstream_and_deleting_preserves_successful_usage(tmp_path):
    provider = ArkTasks()
    with _test_client(tmp_path, provider) as client:
        csrf = _account(client, "owner@example.com")
        owner, another = _new_key(client, csrf), _new_key(client, csrf)
        queued = _submit(client, owner)
        successful = _submit(client, owner)
        running = _submit(client, owner)
        queued_url = f"{TASKS_PATH}/{queued}"
        success_url = f"{TASKS_PATH}/{successful}"

        assert client.delete(queued_url, headers=another).status_code == 404
        provider.drop_delete_response = True
        assert client.delete(queued_url, headers=owner).json() == {}
        assert client.get(queued_url, headers=owner).json()["status"] == "cancelled"
        assert client.delete(queued_url, headers=owner).json() == {}
        assert client.get(queued_url, headers=owner).status_code == 200
        assert client.delete(queued_url + "?delete_cancelled=true", headers=owner).json() == {}
        assert client.get(queued_url, headers=owner).status_code == 404

        provider.states["provider-task-2"] = "succeeded"
        assert client.get(success_url, headers=owner).json()["usage"]["completion_tokens"] == 212
        assert client.delete(success_url, headers=owner).json() == {}
        assert client.delete(success_url, headers=owner).status_code == 200
        assert client.get(success_url, headers=owner).status_code == 404
        assert client.get(TASKS_PATH, headers=owner).json()["total"] == 1
        assert [record["id"] for record in client.get("/api/customer/tasks").json()["items"]] == [running]
        with client.app.state.db.connect() as connection:
            charge = connection.execute("SELECT output_tokens FROM video_charges WHERE task_id = ?", (successful,)).fetchone()
            assert charge[0] == 212

        provider.states["provider-task-3"] = "running"
        assert client.delete(f"{TASKS_PATH}/{running}", headers=owner).status_code == 409
        assert [path for method, path in provider.calls if method == "DELETE"] == [f"{TASKS_PATH}/provider-task-1"]


def test_unconfirmed_cancellation_does_not_mark_task_cancelled(tmp_path):
    provider = ArkTasks()
    provider.cancel_result = "missing"
    with _test_client(tmp_path, provider) as client:
        owner = _new_key(client, _account(client, "owner@example.com"))
        task_id = _submit(client, owner)
        result = client.delete(f"{TASKS_PATH}/{task_id}", headers=owner)
        assert result.status_code == 502
        assert result.json()["detail"]["code"] == "video_cancellation_unknown"
        assert client.delete(f"{TASKS_PATH}/{task_id}", headers=owner).status_code == 502
        assert [method for method, _ in provider.calls].count("DELETE") == 1
        items = client.get(TASKS_PATH, headers=owner).json()["items"]
        assert len(items) == 1 and items[0]["status"] == "queued"
        assert items[0]["cancellation_pending"] is True
        assert client.get(f"{TASKS_PATH}/{task_id}", headers=owner).json() == items[0]
        assert asyncio.run(poll_unfinished(client.app)) == 0


def test_callbacks_retry_in_order_with_stable_event_ids_and_signed_local_payload(tmp_path):
    provider = ArkTasks()
    callback = "https://callback.example.com/events?pipeline=seedance"
    with _test_client(tmp_path, provider, callback_poll_seconds=300) as client:
        owner = _new_key(client, _account(client, "callback@example.com"))
        assert client.post(TASKS_PATH, headers=owner, json={**PAYLOAD, "callback_url": "https://127.0.0.1/"}).status_code == 422
        headers = {**owner, "Idempotency-Key": "signed-fixture"}
        task_id = _submit(client, headers, callback_url=callback)
        assert _submit(client, headers, callback_url=callback) == task_id
        assert client.post(TASKS_PATH, headers=headers, json={**PAYLOAD, "callback_url": "https://other.example.com/"}).status_code == 409
        provider.states["provider-task-1"] = "running"
        task_path = f"{TASKS_PATH}/{task_id}"
        assert client.get(task_path, headers=owner).json()["status"] == "running"
        provider.states["provider-task-1"] = "succeeded"
        final_task = client.get(task_path, headers=owner).json()
        assert final_task["status"] == "succeeded"
        deliveries = []

        async def send(url, event_headers, content):
            assert url == callback
            identifier = event_headers["X-Richi-Event-Id"]
            timestamp = event_headers["X-Richi-Timestamp"]
            signing_key = hashlib.sha256(owner["Authorization"].removeprefix("Bearer ").encode()).digest()
            signature = hmac.new(signing_key, f"{timestamp}.{identifier}.".encode() + content, hashlib.sha256).hexdigest()
            assert event_headers["X-Richi-Signature"] == f"sha256={signature}"
            assert b"provider-task" not in content
            deliveries.append((identifier, json.loads(content)))
            return len(deliveries) != 1

        database = client.app.state.db
        assert asyncio.run(video_callbacks.deliver_due(database, send)) == 1
        assert asyncio.run(video_callbacks.deliver_due(database, send)) == 0
        with database.connect() as connection:
            connection.execute("UPDATE video_callback_events SET due_at = 0")
        for _ in range(3):
            assert asyncio.run(video_callbacks.deliver_due(database, send)) == 1
        assert deliveries[0] == deliveries[1]
        assert [body["status"] for _, body in deliveries] == ["queued", "queued", "running", "succeeded"]
        assert deliveries[-1][1] == final_task
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_task_callbacks").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM video_callback_events").fetchone()[0] == 0


@pytest.mark.parametrize("revoke_project", [False, True])
def test_revoked_access_discards_queued_callbacks_before_delivery(tmp_path, revoke_project):
    provider = ArkTasks()
    with _test_client(tmp_path, provider, callback_poll_seconds=300) as client:
        csrf = _account(client, "callback@example.com")
        owner = _new_key(client, csrf)
        _submit(client, owner, callback_url="https://callback.example.com/events")
        database = client.app.state.db
        if revoke_project:
            project_id = client.get("/api/customer/me").json()["customer"]["projectId"]
            with database.connect() as connection:
                set_seedance_access(connection, project_id, False)
        else:
            key_id = client.get("/api/customer/keys").json()["keys"][0]["id"]
            assert client.delete(f"/api/customer/keys/{key_id}", headers={"X-CSRF-Token": csrf}).status_code == 204

        async def unexpected_send(*_):
            raise AssertionError("revoked key still receives callbacks")

        assert asyncio.run(video_callbacks.deliver_due(database, unexpected_send)) == 0
        with database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_task_callbacks").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM video_callback_events").fetchone()[0] == 0


def test_callback_transport_rejects_private_dns_without_connecting_and_pins_resolved_ip(monkeypatch):
    url = "https://callback.example.com/jobs?source=fixture"
    connections = []

    class Writer:
        def write(self, message):
            assert message.startswith(b"POST /jobs?source=fixture HTTP/1.1\r\nHost: callback.example.com\r\n")
            assert message.endswith(b"\r\n\r\n{}")

        async def drain(self):
            pass

        def close(self):
            pass

    async def dial(address, port, **options):
        connections.append(address)
        assert (address, port, options["server_hostname"]) == ("93.184.216.34", 443, "callback.example.com")
        stream = asyncio.StreamReader()
        stream.feed_data(b"HTTP/1.1 204 No Content\r\n")
        return stream, Writer()

    async def exercise():
        loop = asyncio.get_running_loop()
        ips = ["93.184.216.34", "127.0.0.1", "::ffff:169.254.169.254"]

        async def resolve(host, port, **options):
            assert (host, port) == ("callback.example.com", 443)
            return [
                (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))
                for ip in ips
            ]

        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(asyncio, "open_connection", dial)
        assert not await video_callbacks._post_callback(url, {}, b"{}")
        assert connections == []
        ips[:] = ["93.184.216.34"]
        assert await video_callbacks._post_callback(url, {}, b"{}")
        assert connections == ["93.184.216.34"]

    asyncio.run(exercise())


def test_verified_presets_are_listed_as_local_aliases_and_mapped_for_generation(tmp_path):
    upstream = []

    def provider(request):
        assert request.method == "POST"
        upstream.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "provider-preset-task"})

    configured = {"preset_example": {"name": "Example", "asset_type": "Image", "upstream_id": "asset-example"}}
    with _test_client(tmp_path, provider, ark_preset_assets=configured) as client:
        owner = _new_key(client, _account(client, "preset@example.com"))
        assert client.get("/api/asset/presets").status_code == 401
        response = client.get("/api/asset/presets", headers=owner)
        assert response.status_code == 200
        assert response.json() == {"items": [{
            "id": "preset_example", "name": "Example", "assetType": "Image", "reference": "asset://preset_example",
        }]}
        assert "asset-example" not in response.text

        for ref, kind, status in (
            ("asset://preset_unknown", "image_url", 404),
            ("asset://preset_example", "video_url", 422),
            ("asset://asset-example", "image_url", 422),
        ):
            content = [*PAYLOAD["content"], {"type": kind, kind: {"url": ref}}]
            assert client.post(TASKS_PATH, headers=owner, json={**PAYLOAD, "content": content}).status_code == status
        assert not upstream
        local = {"type": "image_url", "image_url": {"url": "asset://preset_example"}}
        _submit(client, owner, content=[*PAYLOAD["content"], local])
        assert upstream[0]["content"][1]["image_url"]["url"] == "asset://asset-example"
