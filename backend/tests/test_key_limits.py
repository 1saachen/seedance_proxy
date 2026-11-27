import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from fastapi.testclient import TestClient
import httpx

from app import key_limits
from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access
from app.model_api import TASKS_PATH


MODELS = "/v1/models"
KEYS = "/api/customer/keys"
PLAYGROUND_KEY = "/api/customer/playground/key"
PROMPT = {"model": "doubao-seedance-2.5", "content": [{"type": "text", "text": "A quiet morning"}]}
SECOND = key_limits.MICROSECONDS_PER_SECOND


def _client(tmp_path, upstream):
    transport = httpx.MockTransport(upstream)
    return TestClient(create_app(Settings(
        database_path=tmp_path / "key-limits.sqlite", registration_enabled=True, cookie_secure=False,
        ark_api_key="example-ark-key", ark_asset_access_key=None, ark_asset_secret_key=None,
        video_daily_limit=40, video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
    ), ark_transport=transport, ark_asset_transport=transport))


def _no_upstream(request):
    raise AssertionError(f"Unexpected simulated provider call: {request.method}")


def _customer(client):
    response = client.post("/api/customer/register", json={
        "email": "limit-test@example.com", "password": "example-password-123",
    })
    assert response.status_code == 201
    data = response.json()
    with client.app.state.db.connect() as connection:
        set_seedance_access(connection, data["customer"]["projectId"], True)
    return data["customer"]["projectId"], {"X-CSRF-Token": data["csrfToken"]}


def _new_key(client, csrf, name="test key"):
    response = client.post(KEYS, headers=csrf, json={"name": name})
    assert response.status_code == 201
    data = response.json()
    return data["key"]["id"], {"Authorization": f"Bearer {data['secret']}"}


def _limits(client, key_id, *, concurrency=None, rpm=None):
    with client.app.state.db.connect() as connection:
        key_limits.set_limits(connection, key_id, concurrency, rpm)


def _metrics(client, project_id):
    with client.app.state.db.connect() as connection:
        return key_limits.project_key_metrics(connection, project_id)


def _assert_limited(response, code="key_rpm_exceeded"):
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == code


def test_rpm_is_rolling_and_counts_only_one_admission_per_business_request(tmp_path, monkeypatch):
    now = [int(datetime.now(UTC).timestamp()) * SECOND]
    monkeypatch.setattr(key_limits, "_now_us", lambda: now[0])
    calls = []

    def upstream(request):
        calls.append(request.method)
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "provider-first"})

    with _client(tmp_path, upstream) as client:
        project, csrf = _customer(client)
        owner_id, owner = _new_key(client, csrf)
        other_id, other = _new_key(client, csrf)
        _limits(client, owner_id, rpm=2)

        assert client.get(MODELS).status_code == 401
        assert client.get(PLAYGROUND_KEY, headers=owner).status_code == 204
        assert _metrics(client, project)[owner_id]["requestsLastMinute"] == 0
        assert client.get(MODELS, headers=owner).status_code == 200

        now[0] += 300_000
        created = client.post(TASKS_PATH, headers=owner, json=PROMPT)
        assert created.status_code == 200
        assert calls == ["POST"]
        assert _metrics(client, project)[owner_id] == {
            "concurrencyLimit": None, "rpmLimit": 2, "activeTasks": 1, "requestsLastMinute": 2,
        }
        assert client.get("/api/asset-group/list", headers=other).status_code == 200
        assert _metrics(client, project)[other_id]["requestsLastMinute"] == 1

        now[0] += 59_699_999
        blocked = client.get(TASKS_PATH, headers=owner)
        _assert_limited(blocked)
        assert blocked.headers["Retry-After"] == "1"
        assert client.get(PLAYGROUND_KEY, headers=owner).status_code == 204
        assert _metrics(client, project)[owner_id]["requestsLastMinute"] == 2

        now[0] += 1  # Exactly 60 seconds after the first admission.
        assert client.get(TASKS_PATH, headers=owner).status_code == 200
        assert _metrics(client, project)[owner_id]["requestsLastMinute"] == 2
        with client.app.state.db.connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM business_key_requests WHERE key_id = ?", (owner_id,),
            ).fetchone()[0] == 2

        assert client.delete(f"{KEYS}/{owner_id}", headers=csrf).status_code == 204
        assert client.get(MODELS, headers=owner).status_code == 401
        assert _metrics(client, project)[owner_id]["requestsLastMinute"] == 2


def test_rpm_lowering_waits_for_the_necessary_slot_and_keeps_admitted_errors(tmp_path, monkeypatch):
    now = [int(datetime.now(UTC).timestamp()) * SECOND]
    monkeypatch.setattr(key_limits, "_now_us", lambda: now[0])
    with _client(tmp_path, _no_upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        for advance in (0, 20, 20):
            now[0] += advance * SECOND
            missing = client.get("/api/asset-group/get", params={"groupId": "not-owned"}, headers=headers)
            assert missing.status_code == 404

        assert _metrics(client, project)[key_id]["requestsLastMinute"] == 3
        _limits(client, key_id, rpm=1)
        blocked = client.get(MODELS, headers=headers)
        _assert_limited(blocked)
        assert blocked.headers["Retry-After"] == "60"
        now[0] += 60 * SECOND
        assert client.get(MODELS, headers=headers).status_code == 200
        assert _metrics(client, project)[key_id]["requestsLastMinute"] == 1


def test_concurrent_rpm_admissions_do_not_exceed_the_key_limit(tmp_path):
    with _client(tmp_path, _no_upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _limits(client, key_id, rpm=3)

        def request_model(_):
            return client.get(MODELS, headers=headers).status_code

        with ThreadPoolExecutor(max_workers=8) as workers:
            statuses = list(workers.map(request_model, range(8)))
        assert sorted(statuses) == [200] * 3 + [429] * 5
        assert _metrics(client, project)[key_id]["requestsLastMinute"] == 3


def test_concurrency_reservation_blocks_parallel_submissions_and_releases_on_cancel(tmp_path):
    waiting_for_provider = Event()
    release_provider = Event()
    states = {}

    async def upstream(request):
        if request.method == "POST":
            task_id = f"provider-task-{len(states) + 1}"
            states[task_id] = "queued"
            if task_id == "provider-task-1":
                waiting_for_provider.set()
                assert await asyncio.to_thread(release_provider.wait, 10)
            return httpx.Response(200, json={"id": task_id})
        task_id = request.url.path.split("/")[-1]
        if request.method == "DELETE":
            states[task_id] = "cancelled"
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status": states[task_id]})

    with _client(tmp_path, upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _, other = _new_key(client, csrf)
        _limits(client, key_id, concurrency=1)
        first_headers = {**headers, "Idempotency-Key": "first"}
        with ThreadPoolExecutor(max_workers=1) as workers:
            first = workers.submit(client.post, TASKS_PATH, headers=first_headers, json=PROMPT)
            try:
                assert waiting_for_provider.wait(timeout=10)
                assert _metrics(client, project)[key_id]["activeTasks"] == 1
                _assert_limited(client.post(TASKS_PATH, headers=headers, json=PROMPT), "key_concurrency_exceeded")
                pending = client.post(TASKS_PATH, headers=first_headers, json=PROMPT)
                assert pending.json()["detail"]["code"] == "video_submission_pending"
                assert client.post(TASKS_PATH, headers=other, json=PROMPT).status_code == 200
            finally:
                release_provider.set()
            created = first.result(timeout=10)
        assert created.status_code == 200
        task_id = created.json()["id"]
        assert client.post(TASKS_PATH, headers=first_headers, json=PROMPT).json() == {"id": task_id}
        assert len(states) == 2
        assert client.delete(f"{TASKS_PATH}/{task_id}", headers=headers).status_code == 200
        assert _metrics(client, project)[key_id]["activeTasks"] == 0
        assert client.post(TASKS_PATH, headers=headers, json=PROMPT).status_code == 200
        assert len(states) == 3


def test_unknown_concurrency_slot_expires_without_replay_and_queued_slot_remains(tmp_path, monkeypatch):
    origin = datetime.now(UTC).replace(microsecond=0)
    now = [int(origin.timestamp()) * SECOND]
    monkeypatch.setattr(key_limits, "_now_us", lambda: now[0])
    calls = []

    def upstream(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("simulated lost submission", request=request)
        if len(calls) == 2:
            return httpx.Response(400, json={})
        return httpx.Response(200, json={"id": "provider-recovered"})

    with _client(tmp_path, upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _limits(client, key_id, concurrency=1)
        first_headers = {**headers, "Idempotency-Key": "unknown"}
        unknown = client.post(TASKS_PATH, headers=first_headers, json=PROMPT)
        assert unknown.status_code == 502
        task_id = unknown.json()["detail"]["task_id"]
        with client.app.state.db.connect() as connection:
            connection.execute(
                "UPDATE video_tasks SET created_at = ? WHERE id = ?", (origin.strftime("%Y-%m-%d %H:%M:%S"), task_id),
            )
        now[0] += key_limits.UNCONFIRMED_TASK_LIFETIME_SECONDS * SECOND - 1
        _assert_limited(client.post(TASKS_PATH, headers=headers, json=PROMPT), "key_concurrency_exceeded")
        now[0] += 1  # No provider task ID is known and the documented upstream lifetime elapsed.
        assert _metrics(client, project)[key_id]["activeTasks"] == 0
        assert client.post(TASKS_PATH, headers=first_headers, json=PROMPT).json() == unknown.json()
        assert calls == ["POST"]

        failed = client.post(TASKS_PATH, headers=headers, json=PROMPT)
        assert failed.json()["detail"]["code"] == "video_submission_failed"
        assert _metrics(client, project)[key_id]["activeTasks"] == 0
        assert client.post(TASKS_PATH, headers=headers, json=PROMPT).status_code == 200
        now[0] += key_limits.UNCONFIRMED_TASK_LIFETIME_SECONDS * SECOND
        assert _metrics(client, project)[key_id]["activeTasks"] == 1
        _assert_limited(client.post(TASKS_PATH, headers=headers, json=PROMPT), "key_concurrency_exceeded")
        assert calls == ["POST"] * 3
