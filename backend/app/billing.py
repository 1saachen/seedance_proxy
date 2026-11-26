import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext


RESOLUTIONS = ("480p", "720p", "1080p", "4k")
TOKEN_UNIT = 1_000_000
BASIS_POINTS = 10_000
MAX_TOKENS = 10**12  # The maximum validated by the task result parser.
SQLITE_MAX_INTEGER = 2**63 - 1
MAX_UNIT_PRICE = SQLITE_MAX_INTEGER // (MAX_TOKENS // TOKEN_UNIT)

_RESOLUTION_CHECK = f"resolution IN ({', '.join(repr(value) for value in RESOLUTIONS)})"
_PRICE_SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS video_prices (
        id INTEGER PRIMARY KEY,
        upstream_model TEXT NOT NULL,
        resolution TEXT NOT NULL CHECK ({_RESOLUTION_CHECK}),
        contains_video_input INTEGER NOT NULL CHECK (contains_video_input IN (0, 1)),
        effective_at INTEGER NOT NULL CHECK (effective_at >= 0),
        unit_price_micros_per_million INTEGER NOT NULL CHECK (unit_price_micros_per_million >= 0),
        source TEXT NOT NULL,
        UNIQUE(upstream_model, resolution, contains_video_input, effective_at)
    )
"""
_CHARGE_SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS video_charges (
        task_id TEXT PRIMARY KEY REFERENCES video_tasks(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        key_id TEXT NOT NULL REFERENCES business_keys(id),
        model TEXT NOT NULL,
        upstream_model TEXT,
        resolution TEXT CHECK ({_RESOLUTION_CHECK}),
        contains_video_input INTEGER NOT NULL CHECK (contains_video_input IN (0, 1)),
        output_tokens INTEGER CHECK (output_tokens >= 0),
        status TEXT NOT NULL CHECK (status IN ('unbilled', 'pending', 'priced')),
        pending_reason TEXT,
        price_id INTEGER REFERENCES video_prices(id),
        terms_id INTEGER REFERENCES project_billing_terms(id),
        unit_price_micros_per_million INTEGER CHECK (unit_price_micros_per_million >= 0),
        payable_bps INTEGER CHECK (payable_bps BETWEEN 0 AND 10000),
        list_amount_micros INTEGER CHECK (list_amount_micros >= 0),
        amount_micros INTEGER CHECK (amount_micros >= 0),
        task_created_at TEXT NOT NULL,
        settled_at TEXT,
        priced_at TEXT,
        CHECK ((status = 'pending') = (pending_reason IS NOT NULL)),
        CHECK ((status = 'priced') = (priced_at IS NOT NULL)),
        CHECK ((status = 'priced') = (amount_micros IS NOT NULL AND list_amount_micros IS NOT NULL))
    )
"""


def _migrate_4k_resolution(connection: sqlite3.Connection) -> None:
    tables = connection.execute("""
        SELECT sql FROM sqlite_schema WHERE type = 'table' AND name IN ('video_prices', 'video_charges')
    """).fetchall()
    if all("'4k'" in row[0] for row in tables):
        return

    schema_objects = connection.execute("""
        SELECT sql FROM sqlite_schema
        WHERE tbl_name IN ('video_prices', 'video_charges') AND type IN ('index', 'trigger') AND sql IS NOT NULL
        ORDER BY rowid
    """).fetchall()
    # Renaming the child first keeps its old price references valid until both tables are copied.
    connection.execute("ALTER TABLE video_charges RENAME TO video_charges_before_4k")
    connection.execute("ALTER TABLE video_prices RENAME TO video_prices_before_4k")
    connection.execute(_PRICE_SCHEMA)
    connection.execute(_CHARGE_SCHEMA)
    # Only the resolution CHECK changes. Preserve column order, primary keys and all charge values.
    connection.execute("INSERT INTO video_prices SELECT * FROM video_prices_before_4k")
    connection.execute("INSERT INTO video_charges SELECT * FROM video_charges_before_4k")
    connection.execute("DROP TABLE video_charges_before_4k")
    connection.execute("DROP TABLE video_prices_before_4k")
    for (sql,) in schema_objects:
        connection.execute(sql)


def initialize(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_PRICE_SCHEMA)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS project_billing_terms (
                id INTEGER PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                effective_at INTEGER NOT NULL CHECK (effective_at >= 0),
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                payable_bps INTEGER NOT NULL CHECK (payable_bps BETWEEN 0 AND 10000),
                UNIQUE(project_id, effective_at)
            )
        """)
        connection.execute(_CHARGE_SCHEMA)
        _migrate_4k_resolution(connection)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS video_charges_project_settled ON video_charges(project_id, settled_at, task_id)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS video_charges_project_created ON video_charges(project_id, task_created_at, task_id)
        """)


def _check_integer(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")


def add_price(
    connection: sqlite3.Connection, *, upstream_model: str, resolution: str, contains_video_input: bool,
    effective_at: int, unit_price_micros_per_million: int, source: str,
) -> int:
    if not isinstance(upstream_model, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", upstream_model):
        raise ValueError("invalid upstream model")
    if not isinstance(resolution, str) or resolution not in RESOLUTIONS or type(contains_video_input) is not bool:
        raise ValueError("invalid video pricing dimensions")
    if not isinstance(source, str) or not source.strip() or not source.strip().isprintable():
        raise ValueError("price requires a source")
    _check_integer(effective_at, SQLITE_MAX_INTEGER, "effective_at")
    _check_integer(unit_price_micros_per_million, MAX_UNIT_PRICE, "unit_price_micros_per_million")
    source = source.strip()
    dimensions = (upstream_model, resolution, contains_video_input, effective_at)
    added = connection.execute("""
        INSERT INTO video_prices
            (upstream_model, resolution, contains_video_input, effective_at, unit_price_micros_per_million, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(upstream_model, resolution, contains_video_input, effective_at) DO NOTHING RETURNING id
    """, (*dimensions, unit_price_micros_per_million, source)).fetchone()
    if added is not None:
        return added[0]
    existing = connection.execute("""
        SELECT id, unit_price_micros_per_million, source FROM video_prices
        WHERE upstream_model = ? AND resolution = ? AND contains_video_input = ? AND effective_at = ?
    """, dimensions).fetchone()
    if existing[1] != unit_price_micros_per_million or existing[2] != source:
        raise ValueError("a different price already has that effective time")
    return existing[0]


def add_project_terms(
    connection: sqlite3.Connection, *, project_id: str, effective_at: int, enabled: bool, payable_bps: int,
) -> int:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    _check_integer(effective_at, SQLITE_MAX_INTEGER, "effective_at")
    _check_integer(payable_bps, BASIS_POINTS, "payable_bps")
    added = connection.execute("""
        INSERT INTO project_billing_terms (project_id, effective_at, enabled, payable_bps)
        VALUES (?, ?, ?, ?) ON CONFLICT(project_id, effective_at) DO NOTHING RETURNING id
    """, (project_id, effective_at, enabled, payable_bps)).fetchone()
    if added is not None:
        return added[0]
    existing = connection.execute("""
        SELECT id, enabled, payable_bps FROM project_billing_terms WHERE project_id = ? AND effective_at = ?
    """, (project_id, effective_at)).fetchone()
    if existing[1] != enabled or existing[2] != payable_bps:
        raise ValueError("different billing terms already have that effective time")
    return existing[0]


def _output_resolution(result_json: str) -> str | None:
    try:
        result = json.loads(result_json)
    except (TypeError, ValueError):
        return None
    value = result.get("resolution") if isinstance(result, dict) else None
    if value == "4K":
        return "4k"
    return value if isinstance(value, str) and value in RESOLUTIONS else None


def _amounts(output_tokens: int, price: int, payable_bps: int) -> tuple[int, int]:
    with localcontext() as context:
        context.prec = 40
        base = Decimal(output_tokens) * price / TOKEN_UNIT
        payable = base * payable_bps / BASIS_POINTS
        return (
            int(base.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            int(payable.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
        )


def record_task(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Record or complete one successful task's charge in the caller's transaction."""
    if row["status"] != "succeeded":
        return
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute("SELECT * FROM video_charges WHERE task_id = ?", (row["id"],)).fetchone()
    if existing is not None and existing["status"] != "pending":
        return

    tokens = row["output_tokens"]
    output_tokens = tokens if type(tokens) is int and 0 <= tokens <= MAX_TOKENS else None
    upstream_model = row["upstream_model"] or None
    resolution = _output_resolution(row["result_json"])
    settled_at = row["settled_at"] or None
    contains_video_input = row["contains_video_input"]
    created_at = row["created_at"]
    if existing is not None:
        # Keep known source facts and fill only missing values when replaying a pending line.
        output_tokens = existing["output_tokens"] if existing["output_tokens"] is not None else output_tokens
        upstream_model = existing["upstream_model"] or upstream_model
        resolution = existing["resolution"] or resolution
        settled_at = existing["settled_at"] or settled_at
        contains_video_input = existing["contains_video_input"]
        created_at = existing["task_created_at"]

    price_id = None
    unit_price = None
    pending_reason = None
    list_amount = None
    amount = None
    priced_at = None
    status = "unbilled"

    if existing is None:
        terms = connection.execute("""
            SELECT id, enabled, payable_bps FROM project_billing_terms
            WHERE project_id = ? AND effective_at <= CAST(strftime('%s', ?) AS INTEGER)
            ORDER BY effective_at DESC LIMIT 1
        """, (row["project_id"], created_at)).fetchone()
        billing_enabled = terms is not None and bool(terms["enabled"])
        terms_id = terms["id"] if terms is not None else None
        payable_bps = terms["payable_bps"] if terms is not None else None
    else:
        billing_enabled = True  # A pending record already selected an enabled terms version.
        terms_id = existing["terms_id"]
        payable_bps = existing["payable_bps"]

    if billing_enabled:
        if existing is not None and existing["price_id"] is not None:
            price_id, unit_price = existing["price_id"], existing["unit_price_micros_per_million"]
        elif upstream_model and resolution is not None:
            price = connection.execute("""
                SELECT id, unit_price_micros_per_million FROM video_prices
                WHERE upstream_model = ? AND resolution = ? AND contains_video_input = ?
                    AND effective_at <= CAST(strftime('%s', ?) AS INTEGER)
                ORDER BY effective_at DESC LIMIT 1
            """, (upstream_model, resolution, contains_video_input, created_at)).fetchone()
            if price is not None:
                price_id, unit_price = price

        status = "pending"
        if output_tokens is None:
            pending_reason = "missing_usage"
        elif not upstream_model or resolution is None:
            pending_reason = "missing_pricing_dimensions"
        elif unit_price is None:
            pending_reason = "missing_price"
        elif settled_at is None:
            pending_reason = "missing_settlement"
        else:
            status = "priced"
            list_amount, amount = _amounts(output_tokens, unit_price, payable_bps)
            priced_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    connection.execute("""
        INSERT INTO video_charges (
            task_id, project_id, key_id, model, upstream_model, resolution, contains_video_input, output_tokens,
            status, pending_reason, price_id, terms_id, unit_price_micros_per_million, payable_bps,
            list_amount_micros, amount_micros, task_created_at, settled_at, priced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            upstream_model = excluded.upstream_model, resolution = excluded.resolution,
            output_tokens = excluded.output_tokens, status = excluded.status, pending_reason = excluded.pending_reason,
            price_id = excluded.price_id, unit_price_micros_per_million = excluded.unit_price_micros_per_million,
            list_amount_micros = excluded.list_amount_micros, amount_micros = excluded.amount_micros,
            settled_at = excluded.settled_at, priced_at = excluded.priced_at
        WHERE video_charges.status = 'pending'
    """, (
        row["id"], row["project_id"], row["key_id"], row["model"], upstream_model, resolution,
        contains_video_input, output_tokens, status, pending_reason, price_id, terms_id, unit_price,
        payable_bps, list_amount, amount, created_at, settled_at, priced_at,
    ))


def reconcile_batch(
    connection: sqlite3.Connection, *, after_rowid: int = 0, limit: int = 100, include_pending: bool = True,
) -> tuple[int, int]:
    if limit < 1:
        raise ValueError("limit must be positive")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    rows = connection.execute("""
        SELECT t.rowid AS scan_rowid, t.* FROM video_tasks AS t
        LEFT JOIN video_charges AS c ON c.task_id = t.id
        WHERE t.rowid > ? AND t.status = 'succeeded' AND (c.task_id IS NULL OR (? AND c.status = 'pending'))
        ORDER BY t.rowid LIMIT ?
    """, (after_rowid, include_pending, limit)).fetchall()
    for row in rows:
        record_task(connection, row)
    return (rows[-1]["scan_rowid"] if rows else after_rowid), len(rows)
