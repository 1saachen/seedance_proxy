import csv
from datetime import datetime
from io import StringIO

from fastapi.testclient import TestClient

from app import model_access
from test_usage import _app, _owner, _priced_task, _session


REPORT = "/api/customer/usage/report"
DAY = {"from": "2026-12-01", "to": "2026-12-01"}
TEXT_MODEL = "doubao-seed-2.1-lite"
IMAGE_MODEL = "doubao-seedream-5.5"
VIDEO_MODEL = "doubao-seedance-2.5"


def _general_call(
    connection, owner, call_id, kind, created_at, *, status="succeeded",
    prompt=None, completion=None, cached=None, images=None, input_images=None, image_sizes=None,
):
    created = int(datetime.fromisoformat(created_at).timestamp())
    settled = created + 2 if status == "succeeded" else None
    connection.execute("""
        INSERT INTO general_model_calls (
            id, project_id, key_id, modality, model, upstream_model, status, created_at, settled_at,
            prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens, input_images, generated_images,
            image_sizes_json, provider_request_id, cached_result_json
        ) VALUES (?, ?, ?, ?, ?, 'private-upstream-id', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'private-provider-id', '{}')
    """, (
        call_id, *owner[:2], kind, TEXT_MODEL if kind == "text" else IMAGE_MODEL, status, created, settled,
        prompt, completion, prompt + completion if prompt is not None and completion is not None else None,
        cached, input_images, images, image_sizes,
    ))


def _quoted_key(client, owner):
    headers = _session(owner)
    csrf = client.get("/api/customer/me", headers=headers).json()["csrfToken"]
    signed = {**headers, "X-CSRF-Token": csrf}
    created = client.post("/api/customer/keys", headers=signed, json={"name": "=2+3"}).json()
    key = created["key"]
    assert client.delete(f"/api/customer/keys/{key['id']}", headers=signed).status_code == 204
    return (owner[0], key["id"], owner[2]), created["secret"]


def test_report_and_csv_share_filters_scope_metadata_and_known_amounts(tmp_path):
    app = _app(tmp_path / "report.sqlite")
    with TestClient(app) as client:
        owner = _owner(client, "first@example.test")
        alternate, alternate_secret = _quoted_key(client, owner)
        outsider = _owner(client, "second@example.test")
        with app.state.db.connect() as connection:
            _priced_task(connection, owner, "vid_before", "2026-11-30 15:59:59", "priced", 999_000)
            _priced_task(connection, owner, "vid_start", "2026-11-30 16:00:00", "priced", 2_345_678)
            _priced_task(connection, alternate, "vid_zero", "2026-12-01 13:00:00", "priced", 0)
            _priced_task(connection, owner, "vid_pending", "2026-12-01T23:59:59+08:00", "pending")
            connection.execute("INSERT INTO video_task_deletions (task_id) VALUES ('vid_start')")
            _general_call(connection, alternate, "chat_own", "text", "2026-12-01T00:00:00Z",
                          prompt=10, completion=7, cached=5)
            _general_call(connection, owner, "image_own", "image", "2026-12-01T10:00:00Z",
                          images=1, input_images=2, image_sizes='["2048x2048"]')
            _general_call(connection, owner, "chat_failed", "text", "2026-12-01T15:00:00Z", status="failed")
            _general_call(connection, owner, "chat_after", "text", "2026-12-01T16:00:00Z", prompt=200, completion=3)
            _general_call(connection, outsider, "chat_foreign", "text", "2026-12-01T00:00:00Z",
                          prompt=100, completion=10)
            inconsistent = (owner[0], outsider[1], owner[2])
            _general_call(connection, inconsistent, "image_foreign_key", "image", "2026-12-01T00:00:00Z", images=100)
        client.cookies.clear()
        for path in (REPORT, REPORT + ".csv"):
            assert client.get(path, params=DAY).status_code == 401
            assert client.get(path, params=DAY, headers={"Authorization": f"Bearer {alternate_secret}"}).status_code == 401

        params = {**DAY, "pageSize": 2}
        response = client.get(REPORT, params=params, headers=_session(owner))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert all(secret not in response.text for secret in (alternate_secret, outsider[1], "private-upstream-id", "private-provider-id"))
        report = response.json()
        assert report["from"] == DAY["from"] and report["to"] == DAY["to"]
        assert report["total"] == 6 and report["page"] == 1 and report["pageSize"] == 2
        assert report["summary"] == {
            "calls": 6, "successful": 5, "pricedCalls": 2, "pendingCalls": 1, "promptTokens": 10,
            "outputTokens": 3007, "generatedImages": 1, "listAmount": "2.345878", "amount": "2.345678",
        }
        assert [item["id"] for item in report["items"]] == ["vid_pending", "chat_failed"]
        first = report["items"][0]
        assert first["billingStatus"] == "pending" and first["pendingReason"] == "missing_price" and first["amount"] is None
        assert {key["id"]: key["status"] for key in report["keys"]} == {owner[1]: "active", alternate[1]: "revoked"}
        assert {model["id"] for model in report["models"]} == {VIDEO_MODEL, TEXT_MODEL, IMAGE_MODEL}
        page_2 = client.get(REPORT, params={**params, "page": 2}, headers=_session(owner)).json()
        zero, image = page_2["items"]
        assert zero["id"] == "vid_zero" and zero["amount"] == "0.000000"
        assert image["generatedImages"] == 1 and image["inputImages"] == 2 and image["outputImageSizes"] == ["2048x2048"]
        assert image["amount"] is None and image["billingStatus"] == "unbilled"

        filters = {**DAY, "model": TEXT_MODEL, "keyId": alternate[1]}
        selected = client.get(REPORT, params=filters, headers=_session(owner)).json()
        text = selected["items"][0]
        assert selected["total"] == 1 and selected["summary"]["amount"] is None
        assert text["id"] == "chat_own" and text["keyStatus"] == "revoked" and text["cachedPromptTokens"] == 5
        assert text["totalTokens"] == 17 and text["settledAt"] == "2026-12-01T00:00:02Z"

        download = client.get(REPORT + ".csv", params=filters, headers=_session(owner))
        assert download.status_code == 200
        assert download.content.startswith(b"\xef\xbb\xbf")
        assert download.headers["content-type"] == "text/csv; charset=utf-8"
        assert "rich-relay-usage-2026-12-01_2026-12-01.csv" in download.headers["content-disposition"]
        rows = list(csv.DictReader(StringIO(download.content.decode("utf-8-sig"))))
        assert [(row["id"], row["keyName"], row["promptTokens"], row["amount"]) for row in rows] == [
            ("chat_own", "'=2+3", "10", ""),
        ]

        full = client.get(REPORT + ".csv", params=DAY, headers=_session(owner))
        rows = list(csv.DictReader(StringIO(full.content.decode("utf-8-sig"))))
        assert [row["id"] for row in rows] == ["vid_pending", "chat_failed", "vid_zero", "image_own", "chat_own", "vid_start"]
        assert all(identifier not in full.text for identifier in (outsider[1], "private-upstream-id", "private-provider-id"))
        for path in (REPORT, REPORT + ".csv"):
            unknown_key = client.get(path, params={**DAY, "keyId": "key_nonexistent"}, headers=_session(owner))
            foreign_key = client.get(path, params={**DAY, "keyId": outsider[1]}, headers=_session(owner))
            assert unknown_key.status_code == foreign_key.status_code == 404
            assert unknown_key.json() == foreign_key.json()
            assert client.get(path, params={**DAY, "to": "2026-11-30"}, headers=_session(owner)).status_code == 422
            assert client.get(path, params={**DAY, "to": "2026-02-30"}, headers=_session(owner)).status_code == 422


def test_report_redacts_restricted_models_and_does_not_read_the_demo_snapshot(tmp_path):
    app = _app(tmp_path / "scope.sqlite")
    with TestClient(app) as client:
        owner = _owner(client, "first@example.test")
        with app.state.db.connect() as connection:
            model_access.set_seedance_access(connection, owner[0], False)
            _priced_task(connection, owner, "vid_real", "2026-12-01 02:00:00", "priced", 0)
            _general_call(connection, owner, "chat_real", "text", "2026-12-01T03:00:00Z", prompt=2, completion=1)
        app.state.settings.customer_demo_project_id = owner[0]
        app.state.settings.customer_demo_database_path = tmp_path / "missing-demo.sqlite"
        assert client.get("/api/customer/usage", headers=_session(owner)).status_code == 503

        result = client.get(REPORT, params=DAY, headers=_session(owner))
        assert result.status_code == 200 and VIDEO_MODEL not in result.text
        report = result.json()
        assert report["total"] == 2 and report["summary"]["amount"] == "0.000000"
        assert report["models"] == [
            {"kind": "text", "id": TEXT_MODEL, "label": TEXT_MODEL},
            {"kind": "video", "id": "historical_video", "label": model_access.HISTORICAL_MODEL_LABEL},
        ]
        assert [item["model"] for item in report["items"]] == [TEXT_MODEL, model_access.HISTORICAL_MODEL_LABEL]
        hidden = client.get(REPORT, params={**DAY, "model": VIDEO_MODEL}, headers=_session(owner))
        assert hidden.json()["total"] == 0
        visible = client.get(REPORT, params={**DAY, "model": "historical_video"}, headers=_session(owner))
        assert [item["id"] for item in visible.json()["items"]] == ["vid_real"]
        download = client.get(REPORT + ".csv", params=DAY, headers=_session(owner))
        assert VIDEO_MODEL not in download.text and model_access.HISTORICAL_MODEL_LABEL in download.text


def test_report_reads_exact_amounts_beyond_sqlite_integer_sum(tmp_path):
    app = _app(tmp_path / "precision.sqlite")
    with TestClient(app) as client:
        owner = _owner(client, "first@example.test")
        with app.state.db.connect() as connection:
            _priced_task(connection, owner, "vid_high_1", "2026-12-01 00:00:00", "priced", 5_000_000_000_000_000_000)
            _priced_task(connection, owner, "vid_high_2", "2026-12-01 00:00:00", "priced", 4_999_999_999_999_999_999)
        result = client.get(REPORT, params=DAY, headers=_session(owner))
        assert result.status_code == 200
        assert result.json()["summary"]["amount"] == "9999999999999.999999"
