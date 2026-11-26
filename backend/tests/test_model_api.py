import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from app.billing import add_price, add_project_terms
from app.config import Settings
from app.main import create_app
from app.model_api import TASKS_PATH, poll_unfinished


PROMPT = {"model": "doubao-seedance-2.5", "content": [{"type": "text", "text": "Clouds crossing a mountain ridge"}]}
ARK_SECRET = "example-only-ark-key"
VARIANTS = (
    ("doubao-seedance-2.5-flash", "doubao-seedance-2-5-flash-261115", 30, True, (10, 5, 5)),
    ("doubao-seedance-2.0-fast", "doubao-seedance-2-0-fast-260128", 15, False, (9, 3, 3)),
    ("doubao-seedance-2.0-mini", "doubao-seedance-2-0-mini-260615", 15, False, (9, 3, 3)),
)


def _client(tmp_path, upstream, *, limit=5, key=ARK_SECRET):
    app = create_app(
        Settings(database_path=tmp_path / "model.sqlite", registration_enabled=True, cookie_secure=False,
                 ark_api_key=key, video_daily_limit=limit),
        ark_transport=httpx.MockTransport(upstream),
    )
    return TestClient(app)


def _register(client, name="alice"):
    response = client.post("/api/customer/register", json={"email": name + "@example.com", "password": "example-password-123"})
    assert response.status_code == 201, response.text
    return response.json()["csrfToken"]


def _key(client, csrf):
    response = client.post("/api/customer/keys", headers={"X-CSRF-Token": csrf}, json={"name": "video test"})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['secret']}"}


def _no_upstream(request):
    pytest.fail(f"Unexpected upstream {request.method} request")


def test_task_flow_preserves_provider_usage_and_keeps_keys_isolated(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request.method)
        assert request.url.host == "ark.cn-beijing.volces.com"
        assert request.headers["Authorization"] == f"Bearer {ARK_SECRET}"
        if request.method == "POST":
            assert request.url.path == TASKS_PATH
            payload = json.loads(request.content)
            assert payload["model"] == "doubao-seedance-2-5-260628"
            assert payload["content"][1]["role"] == "reference_video"
            assert "Idempotency-Key" not in request.headers
            return httpx.Response(200, json={"id": "provider-task-01"})
        assert request.url.path == TASKS_PATH + "/provider-task-01"
        return httpx.Response(200, json={
            "id": "provider-task-01", "model": "hidden-upstream-model", "status": "succeeded",
            "content": {"video_url": "https://cdn.example.com/result.mp4?signature=sample", "extra": ARK_SECRET},
            "duration": 5, "resolution": "720p", "ratio": "16:9",
            "updated_at": int(datetime.now(timezone.utc).timestamp()),
            "usage": {"completion_tokens": 124_102, "total_tokens": 124_102, "extra": ARK_SECRET},
        })

    with _client(tmp_path, upstream) as client:
        assert client.get("/v1/models").status_code == 401
        csrf = _register(client)
        owner = _key(client, csrf)
        same_project = _key(client, csrf)
        models = client.get("/v1/models", headers=owner).json()["data"]
        assert [model["id"] for model in models] == [
            "doubao-seedance-2.5", "doubao-seedance-2.5-flash",
            "doubao-seedance-2.0", "doubao-seedance-2.0-fast", "doubao-seedance-2.0-mini",
        ]
        body = {**PROMPT, "resolution": "720p", "content": [
            *PROMPT["content"], {"type": "video_url", "video_url": {"url": "https://media.example.com/sample.mp4"}},
        ]}
        headers = {**owner, "Idempotency-Key": "sample-01"}
        created = client.post(TASKS_PATH, headers=headers, json=body)
        assert created.status_code == 200
        task_id = created.json()["id"]
        assert task_id.startswith("vid_")
        assert client.post(TASKS_PATH, headers=headers, json=body).json() == {"id": task_id}
        assert client.post(TASKS_PATH, headers=headers, json={**body, "duration": 6}).status_code == 409
        other_project = _key(client, _register(client, "bob"))
        path = f"{TASKS_PATH}/{task_id}"
        assert client.get(path, headers=same_project).status_code == 404
        assert client.get(path, headers=other_project).status_code == 404
        response = client.get(path, headers=owner)
        assert response.json() == {
            "id": task_id, "model": "doubao-seedance-2.5", "status": "succeeded",
            "content": {"video_url": "https://cdn.example.com/result.mp4?signature=sample"},
            "duration": 5, "resolution": "720p", "ratio": "16:9",
            "usage": {"completion_tokens": 124_102, "total_tokens": 124_102},
        }
        assert "provider-task-01" not in response.text
        assert ARK_SECRET not in response.text
        assert client.get(path, headers=owner).json() == response.json()
        assert calls == ["POST", "GET"]
        with client.app.state.db.connect() as connection:
            row = connection.execute("SELECT * FROM video_tasks WHERE id=?", (task_id,)).fetchone()
            assert row["settled_at"] and row["output_tokens"] == 124_102 and row["contains_video_input"] == 1


def _media_content(media, count):
    return [
        {"type": media, media: {"url": f"https://media.example.com/{media}/{index}"}}
        for index in range(count)
    ]


@pytest.mark.parametrize("alias, upstream_model, provider_resolution", [
    ("doubao-seedance-2.5", "doubao-seedance-2-5-260628", "4k"),
    ("doubao-seedance-2.0", "doubao-seedance-2-0-260128", "4K"),
])
def test_standard_models_create_and_settle_4k_tasks(tmp_path, alias, upstream_model, provider_resolution):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "POST":
            assert request.url.path == TASKS_PATH
            payload = json.loads(request.content)
            assert payload["model"] == upstream_model
            assert payload["resolution"] == "4k"
            return httpx.Response(200, json={"id": "provider-4k"})
        assert request.url.path == f"{TASKS_PATH}/provider-4k"
        return httpx.Response(200, json={
            "id": "provider-4k", "status": "succeeded", "resolution": provider_resolution,
            "content": {"video_url": "https://cdn.example.com/4k.mp4"},
            "updated_at": int(datetime.now(timezone.utc).timestamp()),
            "usage": {"completion_tokens": 120},
        })

    with _client(tmp_path, upstream) as client:
        headers = _key(client, _register(client))
        models = client.get("/v1/models", headers=headers).json()["data"]
        model = next(model for model in models if model["id"] == alias)
        assert model["video"]["resolutions"] == ["480p", "720p", "1080p", "4k"]

        project_id = client.get("/api/customer/me").json()["customer"]["projectId"]
        with client.app.state.db.connect() as connection:
            add_project_terms(connection, project_id=project_id, effective_at=0, enabled=True, payable_bps=10_000)
            add_price(
                connection, upstream_model=upstream_model, resolution="4k", contains_video_input=False,
                effective_at=0, unit_price_micros_per_million=7_000_000, source="test",
            )
        created = client.post(TASKS_PATH, headers=headers, json={**PROMPT, "model": alias, "resolution": "4k"})
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        task = client.get(f"{TASKS_PATH}/{task_id}", headers=headers).json()
        assert task == {
            "id": task_id, "model": alias, "status": "succeeded", "resolution": "4k",
            "content": {"video_url": "https://cdn.example.com/4k.mp4"},
            "usage": {"completion_tokens": 120},
        }
        assert calls == ["POST", "GET"]
        with client.app.state.db.connect() as connection:
            charge = connection.execute(
                "SELECT resolution, status, amount_micros FROM video_charges WHERE task_id = ?", (task_id,),
            ).fetchone()
            assert tuple(charge) == ("4k", "priced", 840)


@pytest.mark.parametrize("alias, upstream_model, maximum, audio_only, limits", VARIANTS)
def test_variant_catalog_validates_limits_and_settles_using_its_own_model_price(
    tmp_path, alias, upstream_model, maximum, audio_only, limits,
):
    image_limit, video_limit, audio_limit = limits
    media_limits = {"image_url": image_limit, "video_url": video_limit, "audio_url": audio_limit}
    references = [item for media, limit in media_limits.items() for item in _media_content(media, limit)]
    body = {
        "model": alias, "duration": maximum, "resolution": "480p",
        "content": [*PROMPT["content"], *references],
    }
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert request.url.path == TASKS_PATH
            assert payload["model"] == upstream_model
            assert payload["duration"] == maximum and payload["resolution"] == "480p"
            assert [item["type"] for item in payload["content"]] == [item["type"] for item in body["content"]]
            return httpx.Response(200, json={"id": "provider-variant"})
        assert request.url.path == f"{TASKS_PATH}/provider-variant"
        return httpx.Response(200, json={
            "id": "provider-variant", "status": "succeeded", "resolution": "480p",
            "content": {"video_url": "https://cdn.example.com/variant.mp4"},
            "updated_at": int(datetime.now(timezone.utc).timestamp()),
            "usage": {"completion_tokens": 42},
        })

    with _client(tmp_path, upstream) as client:
        headers = _key(client, _register(client))
        models = client.get("/v1/models", headers=headers).json()["data"]
        model = next(model for model in models if model["id"] == alias)
        assert model == {
            "id": alias, "object": "model", "owned_by": "volcengine",
            "video": {
                "max_duration": maximum, "resolutions": ["480p", "720p"], "audio_only": audio_only,
                "frame_ratio_adaptive": audio_only, "max_reference_images": image_limit,
                "max_reference_videos": video_limit, "max_reference_audios": audio_limit,
            },
        }
        assert upstream_model not in json.dumps(models)

        invalid = [
            {**body, "resolution": "1080p"},
            {**body, "resolution": "4k"},
            {**body, "duration": maximum + 1},
            *({**body, "content": _media_content(media, limit + 1)} for media, limit in media_limits.items()),
        ]
        if audio_only:
            frame = {**_media_content("image_url", 1)[0], "role": "first_frame"}
            invalid.append({**body, "content": [frame], "ratio": "16:9"})
        else:
            invalid.append({**body, "content": _media_content("audio_url", 1)})
        for payload in invalid:
            response = client.post(TASKS_PATH, headers=headers, json=payload)
            assert response.status_code == 422, response.text
        assert calls == []

        project_id = client.get("/api/customer/me").json()["customer"]["projectId"]
        with client.app.state.db.connect() as connection:
            add_project_terms(connection, project_id=project_id, effective_at=0, enabled=True, payable_bps=10_000)
            add_price(
                connection, upstream_model=upstream_model, resolution="480p", contains_video_input=True,
                effective_at=0, unit_price_micros_per_million=3_000_000, source="test",
            )
        created = client.post(TASKS_PATH, headers=headers, json=body)
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        status = client.get(f"{TASKS_PATH}/{task_id}", headers=headers)
        assert status.status_code == 200
        assert status.json() == {
            "id": task_id, "model": alias, "status": "succeeded", "resolution": "480p",
            "content": {"video_url": "https://cdn.example.com/variant.mp4"},
            "usage": {"completion_tokens": 42},
        }
        assert calls == ["POST", "GET"]
        with client.app.state.db.connect() as connection:
            charge = connection.execute(
                "SELECT upstream_model, status, amount_micros FROM video_charges WHERE task_id = ?", (task_id,),
            ).fetchone()
            assert tuple(charge) == (upstream_model, "priced", 126)


def test_rejects_unknown_options_private_material_and_wrong_model_duration(tmp_path):
    with _client(tmp_path, _no_upstream) as client:
        headers = _key(client, _register(client))
        for payload in (
            {**PROMPT, "channel": "customer-controlled"},
            {**PROMPT, "model": "doubao-seedance-2-5-260628"},
            {**PROMPT, "model": "doubao-seedance-2.0", "duration": 30},
            {**PROMPT, "content": [{"type": "image_url", "image_url": {"url": "asset://unregistered"}}]},
            {**PROMPT, "content": [{"type": "image_url", "image_url": {"url": "https://127.0.0.1/x.png"}}]},
            {**PROMPT, "content": [{"type": "video_url", "video_url": {"url": "https://localhost/x.mp4"}}]},
            {**PROMPT, "content": [{"type": "image_url", "image_url": {"url": "https://name@media.example.com/x.png"}}]},
            {**PROMPT, "content": [{"type": "image_url", "image_url": {"url": "https://media.example.com/b.png"}, "role": "last_frame"}]},
        ):
            response = client.post(TASKS_PATH, headers=headers, json=payload)
            assert response.status_code == 422, response.text
        assert client.post(TASKS_PATH, headers={**headers, "Idempotency-Key": "a b"}, json=PROMPT).status_code == 422


def test_asset_references_require_an_active_matching_asset_in_the_customers_project(tmp_path):
    asset_id = "ast_" + "a" * 32
    group_id = "grp_" + "b" * 32
    local_reference = f"asset://{asset_id}"
    body = {**PROMPT, "content": [{"type": "image_url", "image_url": {"url": local_reference}}]}
    calls = []

    def upstream(request):
        calls.append(request.method)
        payload = json.loads(request.content)
        assert payload["content"][0]["image_url"]["url"] == "asset://asset-provider-01"
        return httpx.Response(200, json={"id": "provider-owned-asset"})

    with _client(tmp_path, upstream, limit=1) as client:
        key = {**_key(client, _register(client)), "Idempotency-Key": "asset-01"}
        project_id = client.get("/api/customer/me").json()["customer"]["projectId"]
        with client.app.state.db.connect() as connection:
            connection.execute(
                "INSERT INTO asset_groups VALUES (?,?,?,'default','Group','',CURRENT_TIMESTAMP)",
                (group_id, project_id, "group-provider-01"),
            )
            connection.execute(
                "INSERT INTO assets VALUES (?,?,?,?, 'Reference', 'Image', 'Processing', CURRENT_TIMESTAMP)",
                (asset_id, project_id, group_id, "asset-provider-01"),
            )
        other_key = _key(client, _register(client, "bob"))
        assert client.post(TASKS_PATH, headers=other_key, json=body).status_code == 404
        assert client.post(TASKS_PATH, headers=key, json=body).status_code == 409
        wrong_type = {**PROMPT, "content": [{"type": "audio_url", "audio_url": {"url": local_reference}}]}
        assert client.post(TASKS_PATH, headers=key, json=wrong_type).status_code == 422
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE assets SET status = 'Active' WHERE id = ?", (asset_id,))
            connection.execute("UPDATE asset_groups SET upstream_project = 'old' WHERE id = ?", (group_id,))
        assert client.post(TASKS_PATH, headers=key, json=body).status_code == 503
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE asset_groups SET upstream_project = 'default' WHERE id = ?", (group_id,))
        created = client.post(TASKS_PATH, headers=key, json=body)
        assert created.status_code == 200
        assert "asset-provider-01" not in created.text
        assert client.post(TASKS_PATH, headers=key, json=body).json() == created.json()
        assert calls == ["POST"]


@pytest.mark.parametrize("scenario, code", [("timeout", "video_submission_unknown"), ("error", "video_submission_failed"),
                                            ("unusable_id", "video_submission_unknown")])
def test_ambiguous_or_rejected_submission_cannot_redispatch(tmp_path, scenario, code):
    calls = []

    def upstream(request):
        calls.append(request.method)
        if scenario == "timeout":
            raise httpx.ReadTimeout(ARK_SECRET, request=request)
        if scenario == "error":
            return httpx.Response(401, json={"message": ARK_SECRET})
        return httpx.Response(200, json={"id": "../../secret"})

    with _client(tmp_path, upstream) as client:
        headers = {**_key(client, _register(client)), "Idempotency-Key": "sample-01"}
        first = client.post(TASKS_PATH, headers=headers, json=PROMPT)
        second = client.post(TASKS_PATH, headers=headers, json=PROMPT)
        assert first.status_code == second.status_code == 502
        detail = first.json()["detail"]
        assert detail["code"] == code
        assert second.json() == first.json()
        assert ARK_SECRET not in first.text
        assert client.get(f"{TASKS_PATH}/{detail['task_id']}", headers=headers).json()["status"].startswith("submission_")
        assert calls == ["POST"]


def test_project_quota_is_atomic_across_keys_and_allows_idempotent_retry(tmp_path):
    started = Event()
    finish = Event()
    calls = []

    async def upstream(request):
        calls.append(request.method)
        started.set()
        if not await asyncio.to_thread(finish.wait, 5):
            pytest.fail("Concurrent assertion did not release the mock request")
        return httpx.Response(200, json={"id": "provider-task-01"})

    with _client(tmp_path, upstream, limit=1) as client, ThreadPoolExecutor(1) as executor:
        csrf = _register(client)
        owner = {**_key(client, csrf), "Idempotency-Key": "quota-01"}
        other_key = _key(client, csrf)
        first = executor.submit(client.post, TASKS_PATH, json=PROMPT, headers=owner)
        try:
            assert started.wait(5)
            replay = client.post(TASKS_PATH, json=PROMPT, headers=owner)
            assert replay.status_code == 409 and replay.json()["detail"]["code"] == "video_submission_pending"
            cap = client.post(TASKS_PATH, json=PROMPT, headers=other_key)
            assert cap.status_code == 429
            assert cap.json()["detail"]["code"] == "video_daily_limit"
        finally:
            finish.set()
        created = first.result(5)
        assert created.status_code == 200
        assert client.post(TASKS_PATH, json=PROMPT, headers=owner).json() == created.json()
        assert calls == ["POST"]


def test_an_upstream_task_cannot_be_assigned_to_two_projects(tmp_path):
    def upstream(request):
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "provider-repeated-id"})

    with _client(tmp_path, upstream) as client:
        owner = _key(client, _register(client))
        first = client.post(TASKS_PATH, json=PROMPT, headers=owner)
        other = _key(client, _register(client, "bob"))
        second = client.post(TASKS_PATH, json=PROMPT, headers=other)
        assert first.status_code == 200 and second.status_code == 502
        assert second.json()["detail"]["code"] == "video_submission_unknown"
        assert client.get(f"{TASKS_PATH}/{first.json()['id']}", headers=other).status_code == 404
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_tasks WHERE upstream_id IS NOT NULL").fetchone()[0] == 1


def test_background_lookup_settles_without_customer_query_and_retains_unknown_tokens(tmp_path):
    updated_at = int(datetime.now(timezone.utc).timestamp())

    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "provider-02"})
        return httpx.Response(200, json={"status": "succeeded", "usage": {"total_tokens": 60}, "updated_at": updated_at})

    with _client(tmp_path, upstream) as client:
        headers = _key(client, _register(client))
        created = client.post(TASKS_PATH, json=PROMPT, headers=headers).json()
        assert client.portal.call(poll_unfinished, client.app) == 1
        assert client.portal.call(poll_unfinished, client.app) == 0
        with client.app.state.db.connect() as connection:
            task = connection.execute("SELECT status, output_tokens, settled_at FROM video_tasks WHERE id=?", (created["id"],)).fetchone()
            assert task["status"] == "succeeded" and task["output_tokens"] is None
            assert task["settled_at"] == datetime.fromtimestamp(updated_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_failed_video_never_records_provider_usage(tmp_path):
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "provider-03"})
        return httpx.Response(200, json={"status": "failed", "usage": {"completion_tokens": 10}, "error": {"message": ARK_SECRET}})

    with _client(tmp_path, upstream) as client:
        headers = _key(client, _register(client))
        created = client.post(TASKS_PATH, headers=headers, json=PROMPT).json()
        failed = client.get(f"{TASKS_PATH}/{created['id']}", headers=headers)
        assert failed.json()["status"] == "failed" and "usage" not in failed.json() and ARK_SECRET not in failed.text
        with client.app.state.db.connect() as connection:
            row = connection.execute("SELECT output_tokens FROM video_tasks WHERE id=?", (created["id"],)).fetchone()
            assert row["output_tokens"] is None


def test_no_models_or_upstream_submission_without_configured_key(tmp_path):
    with _client(tmp_path, _no_upstream, key=None) as client:
        headers = _key(client, _register(client))
        assert client.get("/v1/models", headers=headers).json() == {"object": "list", "data": []}
        assert client.post(TASKS_PATH, json=PROMPT, headers=headers).status_code == 503
