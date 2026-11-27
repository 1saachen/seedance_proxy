from decimal import Decimal
import sqlite3
from typing import Literal

from . import billing, general_models, volcengine_prices


PriceKind = Literal["text", "image"]
PriceTier = tuple[str, str, str]
_PRICE_COLUMNS = """
    id, modality AS kind, upstream_model AS upstreamModel, meter,
    unit_price_micros, source, effective_at,
    strftime('%Y-%m-%dT%H:%M:%SZ', effective_at, 'unixepoch') AS effectiveAt
"""
_VERSION_FILTER = {
    "before": "effective_at < ? ORDER BY effective_at DESC LIMIT 1",
    "exact": "effective_at = ?",
    "after": "effective_at > ? ORDER BY effective_at LIMIT 1",
}
_MODEL_REGISTRY = {"text": general_models.TEXT_MODELS, "image": general_models.IMAGE_MODELS}


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS general_model_prices (
            id INTEGER PRIMARY KEY,
            modality TEXT NOT NULL CHECK (modality IN ('text', 'image')),
            upstream_model TEXT NOT NULL,
            meter TEXT NOT NULL,
            effective_at INTEGER NOT NULL CHECK (effective_at >= 0),
            unit_price_micros INTEGER NOT NULL CHECK (unit_price_micros >= 0),
            source TEXT NOT NULL,
            UNIQUE (modality, upstream_model, meter, effective_at)
        )
    """)


def models(kind: PriceKind) -> list[dict]:
    return [
        {
            "id": model_id,
            "label": alias,
            "tiers": [
                {"meter": meter, "label": label, "unit": volcengine_prices.GENERAL_PRICE_UNITS[kind]}
                for meter, label in volcengine_prices.GENERAL_METERS[kind].get(model_id, {}).items()
            ],
        }
        for alias, model_id in _MODEL_REGISTRY[kind].items()
    ]


def find_version(
    connection: sqlite3.Connection, tier: PriceTier, effective_at: int, relation: Literal["before", "exact", "after"],
) -> sqlite3.Row | None:
    return connection.execute(f"""
        SELECT {_PRICE_COLUMNS}
        FROM general_model_prices
        WHERE modality = ? AND upstream_model = ? AND meter = ? AND {_VERSION_FILTER[relation]}
    """, (*tier, effective_at)).fetchone()


def add_price(
    connection: sqlite3.Connection, *, kind: PriceKind, upstream_model: str, meter: str, effective_at: int,
    unit_price_micros: int, source: str,
) -> int:
    tier = (kind, upstream_model, meter)
    if (
        upstream_model not in _MODEL_REGISTRY[kind].values()
        or meter not in volcengine_prices.GENERAL_METERS[kind].get(upstream_model, {})
    ):
        raise ValueError("invalid model or price meter")
    if type(unit_price_micros) is not int or not 0 <= unit_price_micros <= billing.MAX_UNIT_PRICE:
        raise ValueError("invalid unit price")
    if type(effective_at) is not int or not 0 <= effective_at <= 253402300799:
        raise ValueError("invalid effective time")
    source = source.strip()
    if not source or not source.isprintable() or len(source) > 200:
        raise ValueError("invalid price source")
    inserted = connection.execute("""
        INSERT INTO general_model_prices (modality, upstream_model, meter, effective_at, unit_price_micros, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(modality, upstream_model, meter, effective_at) DO NOTHING RETURNING id
    """, (*tier, effective_at, unit_price_micros, source)).fetchone()
    if inserted is not None:
        return inserted["id"]
    existing = find_version(connection, tier, effective_at, "exact")
    if existing["unit_price_micros"] != unit_price_micros or existing["source"] != source:
        raise ValueError("a different price already has that effective time")
    return existing["id"]


def price_payload(row: sqlite3.Row) -> dict:
    meters = volcengine_prices.GENERAL_METERS[row["kind"]].get(row["upstreamModel"], {})
    return {
        **{key: row[key] for key in ("id", "kind", "upstreamModel", "meter", "effectiveAt", "source")},
        "meterLabel": meters.get(row["meter"], row["meter"]),
        "unit": volcengine_prices.GENERAL_PRICE_UNITS[row["kind"]],
        "unitPrice": format(Decimal(row["unit_price_micros"]).scaleb(-6), ".6f"),
    }


def list_prices(connection: sqlite3.Connection, kind: PriceKind, page: int, page_size: int) -> dict:
    total = connection.execute("SELECT COUNT(*) FROM general_model_prices WHERE modality = ?", (kind,)).fetchone()[0]
    rows = connection.execute(f"""
        SELECT {_PRICE_COLUMNS} FROM general_model_prices WHERE modality = ?
        ORDER BY effective_at DESC, id DESC LIMIT ? OFFSET ?
    """, (kind, page_size, (page - 1) * page_size)).fetchall()
    return {
        "items": [price_payload(row) for row in rows],
        "models": models(kind),
        "total": total, "page": page, "pageSize": page_size,
    }
