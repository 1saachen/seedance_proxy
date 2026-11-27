from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.accounts import SESSION_COOKIE
from app.config import Settings
from app.main import create_app
from app.model_access import set_seedance_access


def _app(path):
    return create_app(Settings(
        database_path=path,
        cookie_secure=False,
        registration_enabled=True,
        ark_api_key=None,
        video_poll_seconds=0,
    ))


def _owner(client, email):
    client.cookies.clear()
    response = client.post("/api/customer/register", json={
        "email": email, "password": "Synthetic-testing-password-42",
    })
    assert response.status_code == 201
    account = response.json()
    with client.app.state.db.connect() as connection:
        set_seedance_access(connection, account["customer"]["projectId"], True)
    key = client.post(
        "/api/customer/keys",
        headers={"X-CSRF-Token": account["csrfToken"]},
        json={"name": "test key"},
    )
    assert key.status_code == 201
    return account["customer"]["projectId"], key.json()["key"]["id"], client.cookies.get(SESSION_COOKIE)


def _session(owner):
    return {"Cookie": f"{SESSION_COOKIE}={owner[2]}"}


def test_usage_only_includes_the_customers_shanghai_month(tmp_path):
    app = _app(tmp_path / "usage.sqlite")
    with TestClient(app) as client:
        first = _owner(client, "first@example.test")
        second = _owner(client, "second@example.test")
        with app.state.db.connect() as connection:
            connection.executemany("""
                INSERT INTO video_tasks
                    (id, project_id, key_id, request_hash, model, status, contains_video_input,
                     output_tokens, created_at)
                VALUES (?, ?, ?, 'synthetic', 'doubao-seedance-2.5', ?, 0, ?, ?)
            """, [
                ("vid_december", *first[:2], "succeeded", 123, "2026-11-30 16:00:00"),
                ("vid_unknown", *first[:2], "succeeded", None, "2026-12-01 00:01:00"),
                ("vid_november", *first[:2], "succeeded", 456, "2026-11-30 15:59:59"),
                ("vid_foreign", *second[:2], "succeeded", 999, "2026-12-01 00:00:00"),
            ])
        client.cookies.clear()
        assert client.get("/api/customer/usage").status_code == 401
        response = client.get("/api/customer/usage?month=2026-12", headers=_session(first))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        usage = response.json()
        assert usage["summary"] == {
            "calls": 2, "successful": 2, "outputTokens": 123, "pricedCalls": 0,
        }
        assert {row["id"]: row["outputTokens"] for row in usage["items"]} == {
            "vid_december": 123, "vid_unknown": None,
        }
        assert (usage["page"], usage["pageSize"]) == (1, 100)
        assert "spendTrend" not in usage and "day" not in usage

        trend = client.get("/api/customer/usage?includeTrend=true", headers=_session(first)).json()["spendTrend"]
        assert trend["totalAmount"] is None and len(trend["days"]) == 7
        assert all(day["amount"] is None and not day["models"] for day in trend["days"])


def _priced_task(connection, owner, task_id, created_at, status, amount=None, *, model="doubao-seedance-2.5"):
    settled_at = "2026-12-08 01:00:00"
    connection.execute("""
        INSERT INTO video_tasks
            (id, project_id, key_id, request_hash, model, status, contains_video_input, output_tokens,
             created_at, settled_at)
        VALUES (?, ?, ?, 'synthetic', ?, 'succeeded', 0, 1000, ?, ?)
    """, (task_id, *owner[:2], model, created_at, settled_at))
    connection.execute("""
        INSERT INTO video_charges
            (task_id, project_id, key_id, model, contains_video_input, output_tokens, status, pending_reason,
             list_amount_micros, amount_micros, task_created_at, settled_at, priced_at)
        VALUES (?, ?, ?, ?, 0, 1000, ?, ?, ?, ?, ?, ?, ?)
    """, (task_id, *owner[:2], model, status, "missing_price" if status == "pending" else None,
          amount + 100 if amount is not None else None, amount, created_at, settled_at,
          settled_at if status == "priced" else None))


def test_spend_trend_crosses_beijing_month_and_is_independent_of_details(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 3, 16, 1, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("app.usage.datetime", FrozenDateTime)
    app = _app(tmp_path / "spend-trend.sqlite")
    with TestClient(app) as client:
        first = _owner(client, "first@example.test")
        second = _owner(client, "second@example.test")
        with app.state.db.connect() as connection:
            _priced_task(connection, first, "vid_before_trend", "2026-11-27 15:59:59", "priced", 900)
            _priced_task(connection, first, "vid_first_day", "2026-11-27 16:00:00", "priced", 5)
            _priced_task(connection, first, "vid_pending_only", "2026-11-29T08:00:00+08:00", "pending")
            _priced_task(connection, first, "vid_unbilled", "2026-11-30 00:00:00", "unbilled")
            for index in range(101):
                _priced_task(connection, first, f"vid_full_day_{index:03}", "2026-11-30 16:00:00", "priced", 1_000_001)
            _priced_task(connection, first, "vid_model_two", "2026-12-01 15:59:59", "priced", 2_000_000,
                         model="doubao-seedance-2.0")
            _priced_task(connection, first, "vid_same_day_pending", "2026-12-01 14:59:59", "pending")
            _priced_task(connection, first, "vid_discount_zero", "2026-12-01 16:00:00", "priced", 0)
            _priced_task(connection, second, "vid_foreign_first", "2026-12-01 00:00:00", "priced", 5_000_000_000_000_000_000)
            _priced_task(connection, second, "vid_foreign_second", "2026-12-01 01:00:00", "priced", 4_999_999_999_999_999_999)
        client.cookies.clear()

        response = client.get("/api/customer/usage", params={"day": "2026-12-01", "includeTrend": "true"}, headers=_session(first))
        assert response.status_code == 200
        usage = response.json()
        assert (usage["month"], usage["day"], usage["page"], usage["pageSize"]) == ("2026-12", "2026-12-01", 1, 100)
        assert usage["summary"] == {"calls": 103, "successful": 103, "outputTokens": 103_000, "pricedCalls": 102}
        assert len(usage["items"]) == 100 and usage["items"][0]["id"] == "vid_model_two"
        trend = usage["spendTrend"]
        assert trend["totalAmount"] == "103.000106"
        assert [day["date"] for day in trend["days"]] == [
            "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04",
        ]
        assert trend["days"][0]["amount"] == "0.000005"
        assert trend["days"][1] == {"date": "2026-11-29", "amount": None, "pricedCalls": 0, "pendingCalls": 1, "models": []}
        assert trend["days"][2] == {"date": "2026-11-30", "amount": None, "pricedCalls": 0, "pendingCalls": 0, "models": []}
        assert trend["days"][3] == {
            "date": "2026-12-01", "amount": "103.000101", "pricedCalls": 102, "pendingCalls": 1,
            "models": [
                {"model": "doubao-seedance-2.0", "amount": "2.000000"},
                {"model": "doubao-seedance-2.5", "amount": "101.000101"},
            ],
        }
        assert trend["days"][4] == {
            "date": "2026-12-02", "amount": "0.000000", "pricedCalls": 1, "pendingCalls": 0,
            "models": [{"model": "doubao-seedance-2.5", "amount": "0.000000"}],
        }

        remaining = client.get("/api/customer/usage", params={"day": "2026-12-01", "page": 2}, headers=_session(first)).json()
        assert remaining["page"] == 2 and remaining["summary"] == usage["summary"]
        assert len(remaining["items"]) == 3
        assert len({item["id"] for page in (usage, remaining) for item in page["items"]}) == 103
        old_day = client.get("/api/customer/usage", params={
            "day": "2026-11-28", "pageSize": 5, "includeTrend": "true",
        }, headers=_session(first)).json()
        assert (old_day["month"], old_day["pageSize"]) == ("2026-11", 5)
        assert [item["id"] for item in old_day["items"]] == ["vid_first_day"]
        assert old_day["spendTrend"] == trend

        isolated = client.get("/api/customer/usage?includeTrend=true", headers=_session(second)).json()
        assert isolated["spendTrend"]["totalAmount"] == "9999999999999.999999"
        mismatch = client.get("/api/customer/usage?day=2026-12-01&month=2026-11", headers=_session(first))
        assert mismatch.status_code == 422 and mismatch.json()["detail"]["code"] == "usage_day_month_mismatch"
        assert client.get("/api/customer/usage?day=2026-02-30", headers=_session(first)).status_code == 422
