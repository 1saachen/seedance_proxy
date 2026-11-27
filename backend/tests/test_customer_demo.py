from contextlib import closing
from datetime import UTC, datetime
import os
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import billing, statements
from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.customer_demo import DEMO_PROJECT_ID
from app.main import create_app


MONTH = "2025-02"
DAY = "2025-02-28"
PROVIDER_MODEL = "seedance-test-model"
USAGE = "/api/customer/usage"
BILLING = "/api/customer/billing"


def _app(path):
    def deny_upstream(request):
        pytest.fail(f"Fixture tests must not call upstream: {request.method}")

    mock = httpx.MockTransport(deny_upstream)
    return create_app(Settings(
        database_path=path, registration_enabled=True, cookie_secure=False,
        ark_api_key=None, ark_asset_access_key=None, ark_asset_secret_key=None,
        video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
    ), ark_transport=mock, ark_asset_transport=mock)


def _register(client, name):
    credentials = {"email": f"{name}@example.test", "password": "Synthetically-Generated-42"}
    response = client.post("/api/customer/register", json=credentials)
    assert response.status_code == 201
    project = response.json()["customer"]["projectId"]
    session = {"Cookie": f"{SESSION_COOKIE}={client.cookies[SESSION_COOKIE]}"}
    client.cookies.clear()
    return project, session, credentials


def _seed(connection, project, task):
    # This timestamp is after midnight on the 28th in Beijing, but still the 27th UTC.
    timestamp = "2025-02-27 16:00:00"
    key = f"key_{task}"
    connection.execute("INSERT OR IGNORE INTO projects VALUES (?, ?)", (project, timestamp))
    connection.execute("""
        INSERT INTO business_keys (id, project_id, name, prefix, secret_hash, created_at)
        VALUES (?, ?, 'synthetic', 'synthetic', ?, ?)
    """, (key, project, f"hash_{task}", timestamp))
    row = connection.execute("""
        INSERT INTO video_tasks (id, project_id, key_id, request_hash, model, upstream_model, status,
                                 result_json, requested_resolution, contains_video_input, output_tokens, created_at, settled_at)
        VALUES (?, ?, ?, 'synthetic', 'doubao-seedance-2.5', ?, 'succeeded',
                '{"resolution":"720p"}', '720p', 0, 100000, ?, ?)
        RETURNING *
    """, (task, project, key, PROVIDER_MODEL, timestamp, timestamp)).fetchone()
    billing.record_task(connection, row)


def _snapshot(tmp_path, real_project, *, confirm=False):
    fixture_app = _app(tmp_path / "fixture-source.sqlite")
    path = tmp_path / "preview snapshot.sqlite"
    with TestClient(fixture_app):
        with fixture_app.state.db.connect() as connection:
            connection.execute("INSERT INTO projects VALUES (?, ?)", (DEMO_PROJECT_ID, DAY))
            billing.add_price(
                connection, upstream_model=PROVIDER_MODEL, resolution="720p", contains_video_input=False,
                effective_at=0, unit_price_micros_per_million=10_000_000, source="Synthetic demo",
            )
            billing.add_project_terms(connection, project_id=DEMO_PROJECT_ID, effective_at=0, enabled=True, payable_bps=7500)
            _seed(connection, DEMO_PROJECT_ID, "vid_preview")
            _seed(connection, real_project, "vid_wrong_fixture_project")
            if confirm:
                result = statements.confirm_statement(
                    connection, DEMO_PROJECT_ID, MONTH, now=datetime(2025, 3, 1, tzinfo=UTC),
                )
                assert result["id"] is not None
        with fixture_app.state.db.connect() as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
    return path


def test_test_project_uses_the_snapshot_through_normal_customer_reads(tmp_path):
    app = _app(tmp_path / "live.sqlite")
    with TestClient(app) as client:
        project, session, credentials = _register(client, "test-account")
        other_project, other, _ = _register(client, "ordinary-customer")
        with app.state.db.connect() as connection:
            _seed(connection, project, "vid_live")
            _seed(connection, other_project, "vid_other_live")
            real_statement = statements.confirm_statement(
                connection, project, MONTH, now=datetime(2025, 3, 1, tzinfo=UTC),
            )
        snapshot = _snapshot(tmp_path, project)
        app.state.settings.customer_demo_project_id = project
        app.state.settings.customer_demo_database_path = snapshot

        for endpoint in (USAGE, BILLING):
            assert client.get(endpoint).status_code == 401

        login = client.post("/api/customer/login", json=credentials)
        assert login.status_code == 200
        for payload in (login.json(), client.get("/api/customer/me", headers=session).json()):
            assert set(payload) == {"customer", "csrfToken"}
        client.cookies.clear()

        default = client.get(USAGE, params={"includeTrend": "true"}, headers=session)
        assert default.status_code == 200 and default.headers["Cache-Control"] == "no-store"
        content = default.json()
        assert "demo" not in content and content["month"] == MONTH
        assert [item["id"] for item in content["items"]] == ["vid_preview"]
        assert content["spendTrend"]["totalAmount"] == "0.750000"
        assert len(content["spendTrend"]["days"]) == 7 and content["spendTrend"]["days"][-1]["date"] == DAY
        chosen = client.get(USAGE, params={"day": DAY}, headers=session).json()
        assert chosen["day"] == DAY and chosen["items"][0]["billing"]["amount"] == "0.750000"
        estimate = client.get(BILLING, headers=session).json()
        assert "demo" not in estimate and estimate["status"] == "estimate" and estimate["id"] is None
        assert estimate["summary"]["payableAmount"] == "0.75" and estimate["summary"]["usageCalls"] == 1

        other_usage = client.get(USAGE, params={"month": MONTH, "demo": "true"}, headers=other).json()
        assert [item["id"] for item in other_usage["items"]] == ["vid_other_live"]
        assert other_usage["items"][0]["billing"]["status"] == "unbilled"
        other_estimate = client.get(BILLING, params={"month": MONTH}, headers=other).json()
        assert other_estimate["summary"]["payableAmount"] == "0.00"
        tasks = client.get("/api/customer/tasks", headers=session).json()
        assert [item["id"] for item in tasks["items"]] == ["vid_live"]
        statement_url = f"{BILLING}/{real_statement['id']}"
        published = client.get(statement_url, headers=session).json()
        assert published["status"] == "confirmed" and published["summary"]["payableAmount"] == "0.00"
        export = client.get(statement_url + "/export.csv", headers=session)
        assert export.status_code == 200 and "vid_preview" not in export.text
        with app.state.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM video_charges").fetchone()[0] == 2
        assert not snapshot.with_name(snapshot.name + "-wal").exists()
        assert not snapshot.with_name(snapshot.name + "-shm").exists()


@pytest.mark.parametrize("kind", ["missing", "same-file", "invalid"])
def test_test_project_fails_closed_if_snapshot_is_unavailable_or_aliases_live_data(tmp_path, kind):
    live = tmp_path / "live.sqlite"
    app = _app(live)
    with TestClient(app) as client:
        project, session, _ = _register(client, "test-account")
        _, other, _ = _register(client, "ordinary-customer")
        fixture = tmp_path / "preview.sqlite"
        if kind == "same-file":
            os.link(live, fixture)
        elif kind == "invalid":
            fixture.write_text("not a standalone SQLite database")
        app.state.settings.customer_demo_project_id = project
        app.state.settings.customer_demo_database_path = fixture
        assert client.get("/api/customer/me", headers=session).status_code == 200
        for endpoint in (USAGE, BILLING):
            response = client.get(endpoint, headers=session)
            assert response.status_code == 503 and response.json()["detail"]["code"] == "usage_unavailable"
            assert client.get(endpoint, headers=other).status_code == 200
        assert fixture.exists() == (kind != "missing")


def test_snapshot_never_returns_a_frozen_invoice(tmp_path):
    app = _app(tmp_path / "live.sqlite")
    with TestClient(app) as client:
        project, session, _ = _register(client, "test-account")
        app.state.settings.customer_demo_project_id = project
        app.state.settings.customer_demo_database_path = _snapshot(tmp_path, project, confirm=True)
        response = client.get(BILLING, headers=session)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "usage_unavailable"
