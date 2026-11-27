from app import accounts, billing, general_model_prices, general_models, model_api, volcengine_prices
from app.db import Database


def test_general_price_schema_preserves_existing_usage_and_video_prices(tmp_path):
    database = Database(tmp_path / "legacy.sqlite")
    text_model = general_models.TEXT_MODELS["doubao-seed-2.1-lite"]
    with database.connect() as connection:
        accounts.initialize(connection)
        model_api.initialize(connection)
        general_models.initialize(connection)
        billing.initialize(connection)
        connection.execute("INSERT INTO projects VALUES ('project_fixture', '2026-11-01T00:00:00Z')")
        connection.execute("""
            INSERT INTO business_keys (id, project_id, name, prefix, secret_hash, created_at)
            VALUES ('key_fixture', 'project_fixture', 'Fixture', 'fixture', 'fixture-hash', '2026-11-01T00:00:00Z')
        """)
        connection.execute("""
            INSERT INTO general_model_calls (
                id, project_id, key_id, modality, model, upstream_model, status,
                prompt_tokens, completion_tokens, total_tokens, created_at
            ) VALUES ('call_fixture', 'project_fixture', 'key_fixture', 'text', 'doubao-seed-2.1-lite', ?,
                      'succeeded', 20, 10, 30, 1793491200)
        """, (text_model,))
        prior_id = billing.add_price(
            connection, upstream_model="doubao-seedance-2-5-260628", resolution="720p", contains_video_input=False,
            effective_at=1793491200, unit_price_micros_per_million=70_000_000, source="Fixture",
        )
        assert not connection.execute("SELECT 1 FROM sqlite_schema WHERE name = 'general_model_prices'").fetchone()

    with database.connect() as connection:
        general_model_prices.initialize(connection)
        assert general_model_prices.list_prices(connection, "text", 1, 25)["total"] == 0
        new_id = general_model_prices.add_price(
            connection, kind="text", upstream_model=text_model, meter="input_tokens",
            effective_at=1795737600, unit_price_micros=400_000, source="Fixture platform rate",
        )

    with database.connect() as connection:
        general_model_prices.initialize(connection)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        prior = connection.execute("SELECT id, unit_price_micros_per_million FROM video_prices").fetchone()
        assert tuple(prior) == (prior_id, 70_000_000)
        usage = connection.execute("SELECT status, prompt_tokens, completion_tokens FROM general_model_calls").fetchone()
        assert tuple(usage) == ("succeeded", 20, 10)
        price = general_model_prices.list_prices(connection, "text", 1, 25)["items"][0]
        assert (price["id"], price["unit"], price["unitPrice"]) == (new_id, "million_tokens", "0.400000")


def test_general_official_prices_cover_registered_meters_and_use_actual_units():
    models = {
        (kind, model["id"], tier["meter"]): tier["unit"]
        for kind in ("text", "image")
        for model in general_model_prices.models(kind)
        for tier in model["tiers"]
    }
    prices = volcengine_prices.GENERAL_PRICES
    assert len(models) == len(prices) == 13
    assert models == {key: price.unit for key, price in prices.items()}
    assert prices["text", "doubao-seed-2-1-pro-260915", "cached_input_tokens"].price_micros == 1_200_000
    assert prices["image", "doubao-seedream-5-5-261028", "output_image_4_63m_to_8_30m"].price_micros == 520_000
    assert prices["image", "doubao-seedream-5-0-pro-260628", "additional_input_image"].price_micros == 20_000
