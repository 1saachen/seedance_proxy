from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_usage_only_includes_the_customers_shanghai_month(tmp_path):
    app = create_app(Settings(
        database_path=tmp_path / "usage.sqlite",
        cookie_secure=False,
        registration_enabled=True,
    ))
    with TestClient(app) as client:
        owners = []
        for email in ("first@example.test", "second@example.test"):
            client.cookies.clear()
            response = client.post("/api/customer/register", json={
                "email": email, "password": "Synthetic-testing-password-42",
            })
            assert response.status_code == 201
            account = response.json()
            key = client.post(
                "/api/customer/keys",
                headers={"X-CSRF-Token": account["csrfToken"]},
                json={"name": "test key"},
            )
            assert key.status_code == 201
            owners.append((account["customer"]["projectId"], key.json()["key"]["id"],
                           client.cookies.get("seedance_customer_session")))
        first, second = owners
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
        response = client.get("/api/customer/usage?month=2026-12", headers={
            "Cookie": f"seedance_customer_session={first[2]}",
        })
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["summary"] == {
            "calls": 2, "successful": 2, "outputTokens": 123, "pricedCalls": 0,
        }
        assert {row["id"]: row["outputTokens"] for row in response.json()["items"]} == {
            "vid_december": 123, "vid_unknown": None,
        }
