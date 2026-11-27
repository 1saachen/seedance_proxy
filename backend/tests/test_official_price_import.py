from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from app import admin_auth, billing, statements, volcengine_prices
from test_admin_operations import _client, _create_project, _customer_key, _login_admin


PRICES = "/api/admin/prices/volcengine"
AUDIT = "/api/admin/audit"
VERIFIED_START = datetime.fromisoformat(volcengine_prices.VERIFIED_ON).replace(tzinfo=statements.BEIJING)
EFFECTIVE = (VERIFIED_START + timedelta(days=4)).isoformat()
FUTURE = (VERIFIED_START + timedelta(days=18)).isoformat()
MAIN_MODEL = "doubao-seedance-2-5-260628"
MAIN_TIER = (MAIN_MODEL, "720p", False)


@pytest.fixture
def operator(tmp_path: Path):
    with _client(tmp_path / "official-prices.sqlite") as client:
        yield client, _login_admin(client)


def _timestamp(value):
    return int(datetime.fromisoformat(value).timestamp())


def _utc_iso(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _price(client, tier, amount, effective_at, source="Synthetic prior price"):
    with client.app.state.db.connect() as connection:
        return billing.add_price(
            connection, upstream_model=tier[0], resolution=tier[1], contains_video_input=tier[2],
            effective_at=effective_at, unit_price_micros_per_million=amount, source=source,
        )


def _preview(client, effective_at=EFFECTIVE):
    return client.get(f"{PRICES}/preview", params={"effectiveAt": effective_at})


def _apply(client, csrf, preview, effective_at=EFFECTIVE):
    return client.post(
        f"{PRICES}/apply", headers=csrf, json={"effectiveAt": effective_at, "previewToken": preview["previewToken"]},
    )


def _tier(preview, key=MAIN_TIER):
    return next(
        item for item in preview["items"]
        if (item["upstreamModel"], item["resolution"], item["containsVideoInput"]) == key
    )


def _count_prices(client):
    with client.app.state.db.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM video_prices").fetchone()[0]


def _import_audits(client):
    return [item for item in client.get(AUDIT).json()["items"] if item["action"] == "billing.price.import"]


def test_official_import_appends_all_changed_versions_once_and_preserves_frozen_bill(operator):
    client, csrf = operator
    prior_id = _price(client, MAIN_TIER, 7_000_000, 0)
    same_tier = (MAIN_MODEL, "480p", True)
    same_price = volcengine_prices.PUBLIC_PRICES[same_tier]
    same_id = _price(client, same_tier, same_price, _timestamp(EFFECTIVE))
    future_id = _price(client, MAIN_TIER, 65_000_000, _timestamp(FUTURE))
    project = _create_project(client, csrf, "import-fixture@example.com")
    key_id = _customer_key(client, project["email"])["key"]["id"]
    with client.app.state.db.connect() as connection:
        terms_id = billing.add_project_terms(
            connection, project_id=project["id"], effective_at=0, enabled=True, payable_bps=7500,
        )
        task = connection.execute("""
            INSERT INTO video_tasks (
                id, project_id, key_id, request_hash, model, upstream_model, status, result_json,
                requested_resolution, contains_video_input, output_tokens, created_at, settled_at
            ) VALUES (
                'vid_synthetic_import', ?, ?, 'fixture', 'doubao-seedance-2.5', ?, 'succeeded',
                '{"resolution":"720p"}', '720p', 0, 1000000, '2025-01-01 00:00:00', '2025-01-01 00:01:00'
            ) RETURNING *
        """, (project["id"], key_id, MAIN_MODEL)).fetchone()
        billing.record_task(connection, task)
        old_bill = statements.confirm_statement(connection, project["id"], "2025-01")
    assert old_bill["summary"]["payableAmount"] == "5.25"

    response = _preview(client)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["sourceUrl"] == volcengine_prices.SOURCE_URL
    assert preview["version"] == volcengine_prices.SNAPSHOT_VERSION
    assert preview["verifiedOn"] == volcengine_prices.VERIFIED_ON
    effective_utc = _utc_iso(datetime.fromisoformat(EFFECTIVE))
    assert preview["effectiveAt"] == effective_utc
    assert preview["canApply"] and len(preview["items"]) == 28
    assert _tier(preview) == {
        "upstreamModel": MAIN_MODEL, "label": "doubao-seedance-2.5", "resolution": "720p",
        "containsVideoInput": False, "unitPrice": "70.000000", "previousUnitPrice": "7.000000",
        "status": "change", "nextEffectiveAt": _utc_iso(datetime.fromisoformat(FUTURE)),
    }
    assert _tier(preview, same_tier)["status"] == "unchanged"
    new_tier = _tier(preview, (MAIN_MODEL, "4k", True))
    assert new_tier["unitPrice"] == "19.000000"
    assert new_tier["previousUnitPrice"] is None and new_tier["status"] == "add"
    assert _count_prices(client) == 3 and _import_audits(client) == []

    imported = _apply(client, csrf, preview, effective_at=effective_utc)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"createdCount": 27, "skippedCount": 1}
    assert _apply(client, csrf, preview).json() == {"createdCount": 0, "skippedCount": 28}
    second_preview = _preview(client).json()
    assert second_preview["previewToken"] == preview["previewToken"]
    assert all(item["status"] == "unchanged" for item in second_preview["items"])
    assert _count_prices(client) == 30

    with client.app.state.db.connect() as connection:
        original_versions = connection.execute(
            "SELECT id, unit_price_micros_per_million, source FROM video_prices WHERE id IN (?, ?, ?) ORDER BY id",
            (prior_id, same_id, future_id),
        ).fetchall()
        assert [tuple(row) for row in original_versions] == [
            (prior_id, 7_000_000, "Synthetic prior price"), (same_id, same_price, "Synthetic prior price"),
            (future_id, 65_000_000, "Synthetic prior price"),
        ]
        charge = connection.execute("SELECT price_id, terms_id, amount_micros FROM video_charges").fetchone()
        assert tuple(charge) == (prior_id, terms_id, 5_250_000)
        assert statements.confirmed_statement(connection, project["id"], old_bill["id"]) == old_bill

    audits = _import_audits(client)
    assert len(audits) == 1
    assert audits[0]["targetType"] == "price_catalog"
    assert audits[0]["targetId"] == volcengine_prices.SNAPSHOT_VERSION
    assert audits[0]["details"] == {
        "version": volcengine_prices.SNAPSHOT_VERSION, "effectiveAt": effective_utc, "createdCount": 27,
    }


def test_same_time_different_price_cannot_be_overwritten(operator):
    client, csrf = operator
    _price(client, MAIN_TIER, 1_000_000, _timestamp(EFFECTIVE))

    preview = _preview(client).json()
    assert not preview["canApply"]
    assert _tier(preview)["status"] == "conflict"
    assert _tier(preview)["previousUnitPrice"] == "1.000000"
    response = _apply(client, csrf, preview)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "price_conflict"
    assert _count_prices(client) == 1 and _import_audits(client) == []


def test_preview_is_stale_after_a_new_future_price_is_scheduled(operator):
    client, csrf = operator
    preview = _preview(client).json()
    _price(client, MAIN_TIER, 65_000_000, _timestamp(FUTURE))
    response = _apply(client, csrf, preview)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "official_price_preview_stale"
    assert _preview(client).json()["previewToken"] != preview["previewToken"]
    assert _count_prices(client) == 1 and _import_audits(client) == []


@pytest.mark.parametrize("failed_table", ["video_prices", "admin_audit"])
def test_import_rolls_back_if_any_price_or_the_audit_fails(operator, failed_table):
    client, csrf = operator
    trigger_sql = {
        "video_prices": """
            CREATE TRIGGER fail_import BEFORE INSERT ON video_prices
            WHEN NEW.resolution = '4k' BEGIN SELECT RAISE(ABORT, 'synthetic import failure'); END
        """,
        "admin_audit": """
            CREATE TRIGGER fail_import BEFORE INSERT ON admin_audit
            WHEN NEW.action = 'billing.price.import' BEGIN SELECT RAISE(ABORT, 'synthetic import failure'); END
        """,
    }
    preview = _preview(client).json()
    with client.app.state.db.connect() as connection:
        connection.execute(trigger_sql[failed_table])

    with pytest.raises(sqlite3.IntegrityError, match="synthetic import failure"):
        _apply(client, csrf, preview)
    assert _count_prices(client) == 0 and _import_audits(client) == []


def test_import_requires_admin_csrf_complete_catalog_and_verified_date(tmp_path, monkeypatch):
    with _client(tmp_path / "official-prices-auth.sqlite") as client:
        assert _preview(client).status_code == 401
        assert _apply(client, {}, {"previewToken": "0" * 64}).status_code == 401
        csrf = _login_admin(client)
        preview = _preview(client).json()
        assert _apply(client, {}, preview).status_code == 403
        assert _preview(client, _utc_iso(VERIFIED_START)).status_code == 200
        early = _preview(client, _utc_iso(VERIFIED_START - timedelta(seconds=1)))
        assert early.status_code == 422 and early.json()["detail"]["code"] == "official_price_effective_too_early"
        assert _preview(client, "2026-12-01T00:00:00").json()["detail"]["code"] == "invalid_effective_at"

        project = _create_project(client, csrf, "price-import-test@example.com")
        _customer_key(client, project["email"])
        incomplete = dict(volcengine_prices.PUBLIC_PRICES)
        del incomplete[MAIN_TIER]
        monkeypatch.setattr(volcengine_prices, "PUBLIC_PRICES", incomplete)
        for result in (_preview(client), _apply(client, csrf, preview)):
            assert result.status_code == 503 and result.json()["detail"]["code"] == "official_price_catalog_incomplete"
        assert _count_prices(client) == 0

        client.cookies.delete(admin_auth.SESSION_COOKIE)
        assert client.get("/api/customer/me").status_code == 200
        assert _preview(client).status_code == 401
        assert _apply(client, csrf, preview).status_code == 401
