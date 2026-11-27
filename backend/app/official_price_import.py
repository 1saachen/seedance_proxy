from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import sqlite3

from . import billing, volcengine_prices


class CatalogIncompleteError(ValueError):
    """The verified catalog does not cover the supported billing dimensions."""


def _effective_iso(seconds: int) -> str:
    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _price_version(
    connection: sqlite3.Connection, dimensions: tuple[str, str, bool], effective_at: int, condition: str,
) -> sqlite3.Row | None:
    return connection.execute("""
        SELECT id, unit_price_micros_per_million, source,
            strftime('%Y-%m-%dT%H:%M:%SZ', effective_at, 'unixepoch') AS effectiveAt,
            effective_at
        FROM video_prices
        WHERE upstream_model = ? AND resolution = ? AND contains_video_input = ?
    """ + condition, (*dimensions, effective_at)).fetchone()


def _yuan(micros: int) -> str:
    return format(Decimal(micros).scaleb(-6), ".6f")


def preview(
    connection: sqlite3.Connection, effective_at: int, models: dict[str, tuple[str, tuple[str, ...]]],
) -> dict:
    snapshot = volcengine_prices.PUBLIC_PRICES
    dimensions = {
        (model_id, resolution, contains_video): label
        for model_id, (label, resolutions) in models.items()
        for resolution in resolutions
        for contains_video in (False, True)
    }
    if (
        not dimensions or dimensions.keys() != snapshot.keys()
        or any(type(price) is not int or not 0 <= price <= billing.MAX_UNIT_PRICE for price in snapshot.values())
    ):
        raise CatalogIncompleteError

    payload = {
        "version": volcengine_prices.SNAPSHOT_VERSION,
        "verifiedOn": volcengine_prices.VERIFIED_ON,
        "sourceUrl": volcengine_prices.SOURCE_URL,
        "effectiveAt": _effective_iso(effective_at),
    }
    state = []
    items = []
    for tier, label in dimensions.items():
        official_price = snapshot[tier]
        previous = _price_version(connection, tier, effective_at, "AND effective_at < ? ORDER BY effective_at DESC LIMIT 1")
        exact = _price_version(connection, tier, effective_at, "AND effective_at = ?")
        future = _price_version(connection, tier, effective_at, "AND effective_at > ? ORDER BY effective_at LIMIT 1")
        active = exact or previous
        active_price = active["unit_price_micros_per_million"] if active is not None else None

        if active_price == official_price:
            status = "unchanged"
        elif exact is not None:
            status = "conflict"
        else:
            status = "add" if previous is None else "change"
        model, resolution, contains_video = tier
        items.append({
            "upstreamModel": model, "label": label, "resolution": resolution,
            "containsVideoInput": contains_video, "unitPrice": _yuan(official_price),
            "previousUnitPrice": _yuan(active_price) if active_price is not None else None, "status": status,
            "nextEffectiveAt": future["effectiveAt"] if future is not None else None,
        })

        # Retrying a completed import can reuse its token. Only versions added by this snapshot
        # at this time are omitted from the comparison; manual or historical changes still invalidate it.
        if exact is not None and active_price == official_price and exact["source"] == volcengine_prices.SOURCE_LABEL:
            exact = None
        state.append((tier, official_price, [tuple(row) if row is not None else None for row in (previous, exact, future)]))

    token_data = json.dumps([payload, volcengine_prices.SOURCE_LABEL, state], separators=(",", ":")).encode()
    return {
        **payload,
        "previewToken": hashlib.sha256(token_data).hexdigest(),
        "items": items,
        "canApply": all(item["status"] != "conflict" for item in items),
    }
