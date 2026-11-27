import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access
from app.model_api import TASKS_PATH


TASK_RECORDS = "/api/customer/tasks"
PLAYGROUND_KEY = "/api/customer/playground/key"
MODEL = "doubao-seedance-2.5"


@pytest.fixture
def client(tmp_path):
    def no_upstream(request):
        pytest.fail(f"Unexpected upstream {request.method} request")

    transport = httpx.MockTransport(no_upstream)
    app = create_app(
        Settings(
            database_path=tmp_path / "customer-tasks.sqlite",
            cookie_secure=False,
            registration_enabled=True,
            video_poll_seconds=0,
            upload_cleanup_seconds=0,
        ),
        ark_transport=transport,
        ark_asset_transport=transport,
    )
    with TestClient(app) as test_client:
        yield test_client


def register(client, name):
    response = client.post("/api/customer/register", json={
        "email": f"{name}@example.test", "password": "Synthetic-testing-password-42",
    })
    assert response.status_code == 201
    payload = response.json()
    with client.app.state.db.connect() as connection:
        set_seedance_access(connection, payload["customer"]["projectId"], True)
    headers = {"Cookie": f"{SESSION_COOKIE}={response.cookies[SESSION_COOKIE]}"}
    client.cookies.clear()
    return payload["customer"]["projectId"], headers, {**headers, "X-CSRF-Token": payload["csrfToken"]}


def issue_key(client, write_headers):
    response = client.post("/api/customer/keys", headers=write_headers, json={"name": "test video key"})
    assert response.status_code == 201
    return response.json()["key"]["id"], {"Authorization": f"Bearer {response.json()['secret']}"}


def test_playground_key_checks_session_ownership_and_revocation(client):
    _, alice, alice_write = register(client, "alice")
    _, _, bob_write = register(client, "bob")
    key_id, own_key = issue_key(client, alice_write)
    _, foreign_key = issue_key(client, bob_write)

    assert client.get(PLAYGROUND_KEY).status_code == 401
    assert client.get(PLAYGROUND_KEY, headers=own_key).status_code == 401
    accepted = client.get(PLAYGROUND_KEY, headers={**alice, **own_key})
    assert accepted.status_code == 204 and accepted.content == b""
    assert accepted.headers["cache-control"] == "no-store"

    wrong_key = {"Authorization": "Bearer vap_live_" + "a" * 43}
    errors = [client.get(PLAYGROUND_KEY, headers={**alice, **headers}) for headers in (
        {}, wrong_key, foreign_key, {"Authorization": "Basic invalid"},
    )]
    assert client.delete(f"/api/customer/keys/{key_id}", headers=alice_write).status_code == 204
    errors.append(client.get(PLAYGROUND_KEY, headers={**alice, **own_key}))
    assert all(response.status_code == 403 for response in errors)
    assert all(response.json() == {
        "detail": {"code": "invalid_api_key", "message": "请提供当前项目有效的业务 API Key"},
    } for response in errors)
    assert client.get("/v1/models", headers=own_key).status_code == 401

    _, active_key = issue_key(client, alice_write)
    with client.app.state.db.connect() as connection:
        connection.execute("UPDATE customer_sessions SET expires_at = 0")
    assert client.get(PLAYGROUND_KEY, headers={**alice, **active_key}).status_code == 401
    assert client.get(TASK_RECORDS, headers=alice).status_code == 401


def test_records_are_project_wide_metadata_and_results_still_require_the_originating_key(client):
    alice_project, alice, alice_write = register(client, "alice")
    bob_project, bob, bob_write = register(client, "bob")
    key_id, own_key = issue_key(client, alice_write)
    sibling_id, sibling_key = issue_key(client, alice_write)
    foreign_id, foreign_key = issue_key(client, bob_write)
    signed_url = "https://media.example.com/test-result.mp4?signature=synthetic-test-signature"
    with client.app.state.db.connect() as connection:
        connection.executemany("""
            INSERT INTO video_tasks
                (id, project_id, key_id, request_hash, model, status, contains_video_input,
                 upstream_id, upstream_model, result_json, output_tokens, created_at, settled_at)
            VALUES (?, ?, ?, 'synthetic', ?, ?, 0, ?, 'private-upstream-model', ?, ?, ?, ?)
        """, [
            ("vid_own", alice_project, key_id, MODEL, "succeeded", "private-provider-success",
             json.dumps({"content": {"video_url": signed_url}}), 123,
             "2026-11-30 16:00:00", "2026-11-30 16:01:00"),
            ("vid_sibling", alice_project, sibling_id, MODEL, "failed", "private-provider-failure",
             json.dumps({"error": {"code": "private-error", "message": "private-provider-message"}}),
             None, "2026-11-30 15:59:59", "2026-11-30 16:02:00"),
            ("vid_foreign", bob_project, foreign_id, MODEL, "succeeded", "private-provider-foreign",
             "{}", 456, "2026-11-30 16:10:00", "2026-11-30 16:11:00"),
        ])

    assert client.get(TASK_RECORDS, headers=own_key).status_code == 401
    listed = client.get(TASK_RECORDS, headers=alice)
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert listed.json() == {"page": 1, "pageSize": 20, "total": 2, "items": [
        {"id": "vid_own", "model": MODEL, "status": "succeeded", "outputTokens": 123,
         "createdAt": "2026-11-30 16:00:00", "settledAt": "2026-11-30 16:01:00", "error": None},
        {"id": "vid_sibling", "model": MODEL, "status": "failed", "outputTokens": None,
         "createdAt": "2026-11-30 15:59:59", "settledAt": "2026-11-30 16:02:00",
         "error": {"code": "video_generation_failed", "message": "视频生成失败"}},
    ]}
    assert client.get(TASK_RECORDS, headers=bob).json()["total"] == 1
    assert client.get(TASK_RECORDS, headers=alice, params={"taskId": "vid_foreign"}).json()["total"] == 0

    result_path = f"{TASKS_PATH}/vid_own"
    assert client.get(result_path, headers=alice).status_code == 401
    assert client.get(result_path, headers=sibling_key).status_code == 404
    assert client.get(result_path, headers=foreign_key).status_code == 404
    assert client.get(result_path, headers=own_key).json()["content"]["video_url"] == signed_url


def test_record_filters_and_pagination_use_beijing_creation_dates(client):
    project, session, write_headers = register(client, "alice")
    key_id, _ = issue_key(client, write_headers)
    statuses = {
        "vid_before": ("succeeded", "2026-11-30 15:59:59"),
        "vid_boundary_a": ("canceled", "2026-11-30 16:00:00"),
        "vid_boundary_b": ("canceled", "2026-12-01T00:00:00+08:00"),
        "vid_with_fraction": ("running", "2026-11-30T16:00:00.250Z"),
        "vid_submitting": ("submitting", "2026-12-01 00:00:00"),
        "vid_unknown": ("submission_unknown", "2026-12-01 01:00:00"),
        "vid_rejected": ("submission_failed", "2026-12-01 02:00:00"),
        "vid_queued": ("queued", "2026-12-01 03:00:00"),
        "vid_failed": ("failed", "2026-12-01 04:00:00"),
        "vid_expired": ("expired", "2026-12-01T23:59:59+08:00"),
        "vid_after": ("succeeded", "2026-12-01 16:00:00"),
    }
    with client.app.state.db.connect() as connection:
        connection.executemany("""
            INSERT INTO video_tasks
                (id, project_id, key_id, request_hash, model, status, contains_video_input, created_at)
            VALUES (?, ?, ?, 'synthetic', ?, ?, 0, ?)
        """, [(task_id, project, key_id, MODEL, *details) for task_id, details in statuses.items()])

    def records(**params):
        response = client.get(TASK_RECORDS, headers=session, params=params)
        assert response.status_code == 200
        return response.json()

    same_day = {"fromDate": "2026-12-01", "toDate": "2026-12-01"}
    during_day = records(**same_day, pageSize=100)
    assert during_day["total"] == 9
    assert [item["id"] for item in during_day["items"]] == [
        "vid_expired", "vid_failed", "vid_queued", "vid_rejected", "vid_unknown", "vid_submitting",
        "vid_with_fraction", "vid_boundary_b", "vid_boundary_a",
    ]
    assert records(toDate="2026-11-30")["items"][0]["id"] == "vid_before"
    assert records(fromDate="2026-12-02")["items"][0]["id"] == "vid_after"

    canceled = {**same_day, "status": "canceled", "pageSize": 1}
    second_page = records(**canceled, page=2)
    assert second_page == {"items": [{
        "id": "vid_boundary_a", "model": MODEL, "status": "canceled", "outputTokens": None,
        "createdAt": "2026-11-30 16:00:00", "settledAt": None,
        "error": {"code": "video_task_canceled", "message": "视频任务已取消"},
    }], "page": 2, "pageSize": 1, "total": 2}
    assert records(**canceled, page=3) == {"items": [], "page": 3, "pageSize": 1, "total": 2}
    assert records(**canceled, page=10**25)["total"] == 2
    assert records(**canceled, taskId="vid_boundary_b")["total"] == 1
    assert records(**canceled, taskId="vid_boundary")["total"] == 0
    assert records(**canceled, taskId="vid_queued")["total"] == 0
    for task_id, (status, _) in statuses.items():
        assert records(taskId=task_id, status=status)["items"][0]["status"] == status

    for params in (
        {"taskId": "missing-prefix"}, {"fromDate": "2026-12-1"}, {"fromDate": "2026-02-30"},
        {"toDate": "2026-12-01T00:00:00Z"}, {"status": "cancelled"}, {"page": 0}, {"pageSize": 101},
    ):
        error = client.get(TASK_RECORDS, headers=session, params=params)
        assert error.status_code == 422
        assert error.json()["detail"]["code"] == "invalid_request"
    reverse = client.get(TASK_RECORDS, headers=session, params={"fromDate": "2026-12-02", "toDate": "2026-12-01"})
    assert reverse.status_code == 422
    assert reverse.json()["detail"]["code"] == "invalid_date_range"
