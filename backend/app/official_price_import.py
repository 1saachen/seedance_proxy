from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import sqlite3
from typing import Literal

from . import billing, general_model_prices, volcengine_prices


PriceScope = Literal["all", "video", "text", "image"]
PriceRelation = Literal["before", "exact", "after"]
MAX_PLATFORM_PAYABLE_BPS = 100_000
VIDEO_PRICE_CONDITIONS = {
    "before": "AND effective_at < ? ORDER BY effective_at DESC LIMIT 1",
    "exact": "AND effective_at = ?",
    "after": "AND effective_at > ? ORDER BY effective_at LIMIT 1",
}


class CatalogIncompleteError(ValueError):
    """The verified catalog does not cover the supported billing dimensions."""


class PriceOutOfRangeError(ValueError):
    """Applying the selected percentage would exceed the supported unit price."""


def _effective_iso(seconds: int) -> str:
    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _price_version(
    connection: sqlite3.Connection, tier: dict, effective_at: int, relation: PriceRelation,
) -> sqlite3.Row | None:
    if tier["kind"] != "video":
        return general_model_prices.find_version(
            connection, (tier["kind"], tier["upstreamModel"], tier["meter"]), effective_at, relation,
        )
    return connection.execute("""
        SELECT id, unit_price_micros_per_million AS unit_price_micros, source,
            strftime('%Y-%m-%dT%H:%M:%SZ', effective_at, 'unixepoch') AS effectiveAt,
            effective_at
        FROM video_prices
        WHERE upstream_model = ? AND resolution = ? AND contains_video_input = ?
    """ + VIDEO_PRICE_CONDITIONS[relation], (
        tier["upstreamModel"], tier["resolution"], tier["containsVideoInput"], effective_at,
    )).fetchone()


def _yuan(micros: int) -> str:
    return format(Decimal(micros).scaleb(-6), ".6f")


def source_label(payable_bps: int) -> str:
    if payable_bps == billing.BASIS_POINTS:
        return volcengine_prices.SOURCE_LABEL
    return f"{volcengine_prices.SOURCE_LABEL}（平台基础价比例 {Decimal(payable_bps) / 100}%）"


def _video_tiers(models: dict[str, tuple[str, tuple[str, ...]]]) -> list[tuple[dict, int]]:
    snapshot = volcengine_prices.PUBLIC_PRICES
    dimensions = {
        (model_id, resolution, contains_video): label
        for model_id, (label, resolutions) in models.items()
        for resolution in resolutions
        for contains_video in (False, True)
    }
    if not dimensions or dimensions.keys() != snapshot.keys():
        raise CatalogIncompleteError
    return [
        ({
            "kind": "video", "unit": "million_tokens", "upstreamModel": model, "label": label,
            "resolution": resolution, "containsVideoInput": contains_video,
        }, snapshot[(model, resolution, contains_video)])
        for (model, resolution, contains_video), label in dimensions.items()
    ]


def _general_tiers(kind: Literal["text", "image"]) -> list[tuple[dict, int]]:
    models = general_model_prices.models(kind)
    if any(not model["tiers"] for model in models):
        raise CatalogIncompleteError
    dimensions = {
        (kind, model["id"], tier["meter"]): (model["label"], tier)
        for model in models
        for tier in model["tiers"]
    }
    snapshot = {key: rate for key, rate in volcengine_prices.GENERAL_PRICES.items() if key[0] == kind}
    if not dimensions or dimensions.keys() != snapshot.keys():
        raise CatalogIncompleteError
    tiers = []
    for key, (model_label, meter) in dimensions.items():
        _, model, meter_id = key
        official_tier = snapshot[key]
        if official_tier.unit != meter["unit"]:
            raise CatalogIncompleteError
        tiers.append(({
            "kind": kind, "upstreamModel": model, "label": model_label, "meter": meter_id,
            "meterLabel": meter["label"], "unit": meter["unit"],
        }, official_tier.price_micros))
    return tiers


def preview(
    connection: sqlite3.Connection, effective_at: int, models: dict[str, tuple[str, tuple[str, ...]]],
    *, kind: PriceScope = "video", payable_bps: int = billing.BASIS_POINTS,
) -> dict:
    tiers = []
    if kind in {"all", "video"}:
        tiers.extend(_video_tiers(models))
    if kind in {"all", "text"}:
        tiers.extend(_general_tiers("text"))
    if kind in {"all", "image"}:
        tiers.extend(_general_tiers("image"))
    if any(type(price) is not int or not 0 <= price <= billing.MAX_UNIT_PRICE for _, price in tiers):
        raise CatalogIncompleteError

    payload = {
        "version": volcengine_prices.SNAPSHOT_VERSION,
        "verifiedOn": volcengine_prices.VERIFIED_ON,
        "sourceUrl": volcengine_prices.SOURCE_URL,
        "effectiveAt": _effective_iso(effective_at),
        "kind": kind,
        "payableBps": payable_bps,
    }
    source = source_label(payable_bps)
    state = []
    items = []
    for tier, official_price in tiers:
        target = (official_price * payable_bps + billing.BASIS_POINTS // 2) // billing.BASIS_POINTS
        if target > billing.MAX_UNIT_PRICE:
            raise PriceOutOfRangeError
        previous = _price_version(connection, tier, effective_at, "before")
        exact = _price_version(connection, tier, effective_at, "exact")
        future = _price_version(connection, tier, effective_at, "after")
        active = exact or previous
        active_price = active["unit_price_micros"] if active is not None else None

        if active_price == target:
            status = "unchanged"
        elif exact is not None:
            status = "conflict"
        else:
            status = "add" if previous is None else "change"
        items.append({
            **tier, "officialUnitPrice": _yuan(official_price), "unitPrice": _yuan(target),
            "previousUnitPrice": _yuan(active_price) if active_price is not None else None, "status": status,
            "nextEffectiveAt": future["effectiveAt"] if future is not None else None,
        })

        # Retrying a completed import can reuse its token. Only versions added by this snapshot
        # at this time are omitted from the comparison; manual or historical changes still invalidate it.
        if exact is not None and active_price == target and exact["source"] == source:
            exact = None
        state.append((tier, official_price, [tuple(row) if row is not None else None for row in (previous, exact, future)]))

    token_data = json.dumps([payload, source, state], separators=(",", ":")).encode()
    return {
        **payload,
        "previewToken": hashlib.sha256(token_data).hexdigest(),
        "items": items,
        "canApply": all(item["status"] != "conflict" for item in items),
    }
