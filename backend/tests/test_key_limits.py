import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
CHAT_PATH = "/v1/chat/completions"
IMAGE_PATH = "/v1/images/generations"
CHAT = {"model": "doubao-seed-2.1-lite", "messages": [{"role": "user", "content": "Hello"}]}
IMAGE = {"model": "doubao-seedream-5.5", "prompt": "Draw a quiet morning"}
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
            "concurrencyLimit": None, "rpmLimit": 2, "activeTasks": 1, "activeTextRequests": 0,
            "activeImageRequests": 0, "activeConcurrentRequests": 1, "requestsLastMinute": 2,
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


def test_shared_concurrency_counts_image_and_video_and_replays_without_a_slot(tmp_path):
    waiting_for_image = Event()
    release_image = Event()
    calls = {"image": 0, "video": 0, "text": 0}

    async def upstream(request):
        if request.url.path.endswith(IMAGE_PATH.removeprefix("/v1")):
            calls["image"] += 1
            waiting_for_image.set()
            assert await asyncio.to_thread(release_image.wait, 10)
            return httpx.Response(200, json={"data": [{"url": "https://example.com/image.png"}]})
        if request.url.path == TASKS_PATH:
            calls["video"] += 1
            return httpx.Response(200, json={"id": f"provider-video-{calls['video']}"})
        assert request.url.path.endswith(CHAT_PATH.removeprefix("/v1"))
        calls["text"] += 1
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
        })

    with _client(tmp_path, upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _, other = _new_key(client, csrf)
        _limits(client, key_id, concurrency=2)
        image_headers = {**headers, "Idempotency-Key": "shared-image"}
        with ThreadPoolExecutor(max_workers=1) as workers:
            image = workers.submit(client.post, IMAGE_PATH, headers=image_headers, json=IMAGE)
            try:
                assert waiting_for_image.wait(10)
                assert client.post(TASKS_PATH, headers=headers, json=PROMPT).status_code == 200
                assert _metrics(client, project)[key_id] == {
                    "concurrencyLimit": 2, "rpmLimit": None, "activeTasks": 1, "activeTextRequests": 0,
                    "activeImageRequests": 1, "activeConcurrentRequests": 2, "requestsLastMinute": 2,
                }
                for path, payload in ((CHAT_PATH, CHAT), (IMAGE_PATH, IMAGE), (TASKS_PATH, PROMPT)):
                    _assert_limited(client.post(path, headers=headers, json=payload), "key_concurrency_exceeded")
                replay = client.post(IMAGE_PATH, headers=image_headers, json=IMAGE)
                assert replay.status_code == 409 and replay.json()["detail"]["code"] == "image_request_pending"
                assert client.post(CHAT_PATH, headers=other, json=CHAT).status_code == 200
            finally:
                release_image.set()
            completed = image.result(timeout=10)
        assert completed.status_code == 200
        assert _metrics(client, project)[key_id]["activeConcurrentRequests"] == 1
        assert client.post(CHAT_PATH, headers=headers, json=CHAT).status_code == 200
        assert client.post(TASKS_PATH, headers=headers, json=PROMPT).status_code == 200
        assert client.post(IMAGE_PATH, headers=image_headers, json=IMAGE).json() == completed.json()
        assert calls == {"image": 1, "video": 2, "text": 2}
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM general_model_calls WHERE key_id = ?", (key_id,)).fetchone()[0] == 2


def test_shared_concurrency_serializes_new_chat_and_video_across_app_instances(tmp_path):
    entered = Event()
    release = Event()
    calls = []

    async def upstream(request):
        calls.append(request.url.path)
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        if request.url.path == TASKS_PATH:
            return httpx.Response(200, json={"id": "provider-video"})
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
        })

    with _client(tmp_path, upstream) as client, _client(tmp_path, upstream) as other:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _limits(client, key_id, concurrency=1)
        with ThreadPoolExecutor(max_workers=2) as workers:
            chat = workers.submit(client.post, CHAT_PATH, headers=headers, json=CHAT)
            video = workers.submit(other.post, TASKS_PATH, headers=headers, json=PROMPT)
            try:
                assert entered.wait(10)
                done, pending = wait((chat, video), timeout=5, return_when=FIRST_COMPLETED)
                assert len(done) == len(pending) == 1
                _assert_limited(done.pop().result(), "key_concurrency_exceeded")
                assert _metrics(client, project)[key_id]["activeConcurrentRequests"] == 1
            finally:
                release.set()
            assert pending.pop().result(timeout=10).status_code == 200
        assert len(calls) == 1


def test_abandoned_general_reservation_expires_without_a_new_general_request(tmp_path, monkeypatch):
    created = int(datetime.now(UTC).timestamp())
    now = [created * SECOND]
    monkeypatch.setattr(key_limits, "_now_us", lambda: now[0])

    def upstream(request):
        assert request.url.path == TASKS_PATH
        return httpx.Response(200, json={"id": "provider-after-general-deadline"})

    with _client(tmp_path, upstream) as client:
        project, csrf = _customer(client)
        key_id, headers = _new_key(client, csrf)
        _limits(client, key_id, concurrency=1)
        with client.app.state.db.connect() as connection:
            connection.execute("""
                INSERT INTO general_model_calls (id, project_id, key_id, modality, model, upstream_model, status, created_at)
                VALUES ('old-general', ?, ?, 'image', 'fixture', 'fixture', 'in_progress', ?)
            """, (project, key_id, created))
        lease_seconds = created - key_limits.general_request_cutoff(created)
        now[0] += lease_seconds * SECOND - 1
        assert _metrics(client, project)[key_id]["activeImageRequests"] == 1
        _assert_limited(client.post(TASKS_PATH, headers=headers, json=PROMPT), "key_concurrency_exceeded")
        now[0] += 1
        assert _metrics(client, project)[key_id]["activeConcurrentRequests"] == 0
        assert client.post(TASKS_PATH, headers=headers, json=PROMPT).status_code == 200
        assert _metrics(client, project)[key_id]["activeTasks"] == 1
