import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import ssl
import time
from urllib.parse import urlsplit

from .db import Database
from .urls import is_public_https


logger = logging.getLogger(__name__)
CALLBACK_RETRY_SECONDS = (5, 30, 120, 600, 1800)
CALLBACK_LEASE_SECONDS = 45
CALLBACK_TIMEOUT_SECONDS = 10
_PUBLIC_STATUS = {"canceled": "cancelled"}
_FINAL_STATUSES = ("succeeded", "failed", "canceled", "expired", "submission_failed", "submission_unknown")
_RESPONSE_STATUS = re.compile(rb"HTTP/1\.[01] (\d{3})(?:[ \r\n])")
CallbackSender = Callable[[str, dict[str, str], bytes], Awaitable[bool]]


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS video_task_callbacks (
            task_id TEXT PRIMARY KEY REFERENCES video_tasks(id),
            url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS video_callback_events (
            sequence INTEGER PRIMARY KEY,
            event_id TEXT UNIQUE NOT NULL,
            task_id TEXT NOT NULL REFERENCES video_task_callbacks(task_id),
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            due_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
            lease_until INTEGER NOT NULL DEFAULT 0,
            UNIQUE(task_id, status)
        );
        CREATE INDEX IF NOT EXISTS video_callback_events_due ON video_callback_events(due_at, sequence);
        CREATE TRIGGER IF NOT EXISTS video_task_callback_status
        AFTER UPDATE OF status ON video_tasks
        WHEN OLD.status <> NEW.status
         AND NEW.status IN ('queued', 'running', 'succeeded', 'failed', 'canceled', 'expired', 'submission_failed', 'submission_unknown')
         AND EXISTS (SELECT 1 FROM video_task_callbacks WHERE task_id = NEW.id)
        BEGIN
            INSERT OR IGNORE INTO video_callback_events (event_id, task_id, status, result_json)
            VALUES (lower(hex(randomblob(16))), NEW.id, NEW.status, NEW.result_json);
        END;
    """)


def register_callback(connection: sqlite3.Connection, task_id: str, url: str) -> None:
    connection.execute("INSERT INTO video_task_callbacks (task_id, url) VALUES (?, ?)", (task_id, url))


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not any((address.is_multicast, address.is_reserved, address.is_loopback))


async def _callback_addresses(host: str) -> list[str]:
    entries = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    addresses = list(dict.fromkeys(entry[4][0] for entry in entries))
    return addresses if addresses and all(_public_address(address) for address in addresses) else []


async def _post_callback(url: str, headers: dict[str, str], body: bytes) -> bool:
    if not is_public_https(url):
        return False
    destination = urlsplit(url)
    host = destination.hostname
    if host is None:
        return False
    route = destination.path or "/"
    if destination.query:
        route += "?" + destination.query
    header_text = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    message = (
        f"POST {route} HTTP/1.1\r\nHost: {destination.netloc}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n{header_text}\r\n"
    ).encode("ascii") + body
    try:
        async with asyncio.timeout(CALLBACK_TIMEOUT_SECONDS):
            addresses = await _callback_addresses(host)
            if not addresses:
                return False
            # Use validated numeric addresses for the connection, while keeping TLS verification on the original host.
            context = ssl.create_default_context()
            for address in addresses:
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(
                        address, 443, ssl=context, server_hostname=host, limit=4096,
                    ), timeout=3)
                except (OSError, TimeoutError):
                    continue
                try:
                    writer.write(message)
                    await writer.drain()
                    match = _RESPONSE_STATUS.match(await reader.readline())
                    return match is not None and 200 <= int(match[1]) < 300
                finally:
                    writer.close()
    except (OSError, TimeoutError, ValueError):
        return False
    return False


def _claim(database: Database, now: int) -> list[sqlite3.Row]:
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        revoked_tasks = """SELECT callback.task_id FROM video_task_callbacks callback
                           JOIN video_tasks task ON task.id = callback.task_id
                           JOIN business_keys account_key ON account_key.id = task.key_id
                           WHERE account_key.revoked_at IS NOT NULL"""
        connection.execute(f"DELETE FROM video_callback_events WHERE task_id IN ({revoked_tasks})")
        connection.execute(f"DELETE FROM video_task_callbacks WHERE task_id IN ({revoked_tasks})")
        claimed = connection.execute("""
            UPDATE video_callback_events SET lease_until = ?
            WHERE sequence IN (
                SELECT sequence FROM video_callback_events
                WHERE due_at <= ? AND lease_until <= ?
                  AND NOT EXISTS (SELECT 1 FROM video_callback_events earlier
                                  WHERE earlier.task_id = video_callback_events.task_id
                                    AND earlier.sequence < video_callback_events.sequence)
                ORDER BY due_at, sequence LIMIT 4
            ) RETURNING sequence
        """, (now + CALLBACK_LEASE_SECONDS, now, now)).fetchall()
        if not claimed:
            return []
        placeholders = ", ".join("?" for _ in claimed)
        return connection.execute(f"""
            SELECT event.*, callback.url, task.model, task.created_at, task.is_draft,
                   task.draft_task_id, task.safety_identifier_hash, account_key.secret_hash
            FROM video_callback_events event
            JOIN video_task_callbacks callback USING (task_id)
            JOIN video_tasks task ON task.id = event.task_id
            JOIN business_keys account_key ON account_key.id = task.key_id
            WHERE event.sequence IN ({placeholders})
        """, tuple(row[0] for row in claimed)).fetchall()


def _event_payload(event: sqlite3.Row, now: int) -> tuple[dict[str, str], bytes]:
    result = json.loads(event["result_json"])
    result.setdefault("created_at", int(datetime.fromisoformat(event["created_at"]).replace(tzinfo=UTC).timestamp()))
    if event["is_draft"]:
        result["draft"] = True
    if event["draft_task_id"]:
        result["draft_task_id"] = event["draft_task_id"]
    if event["safety_identifier_hash"]:
        result["safety_identifier"] = event["safety_identifier_hash"]
    body = json.dumps({
        **result, "id": event["task_id"], "model": event["model"],
        "status": _PUBLIC_STATUS.get(event["status"], event["status"]),
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = f"{now}.{event['event_id']}.".encode("ascii")
    signature = hmac.new(bytes.fromhex(event["secret_hash"]), prefix + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Richi-Event-Id": event["event_id"],
        "X-Richi-Timestamp": str(now),
        "X-Richi-Signature": f"sha256={signature}",
    }, body


def _finish(database: Database, event: sqlite3.Row, delivered: bool, now: int) -> None:
    with database.connect() as connection:
        if delivered or event["attempts"] >= len(CALLBACK_RETRY_SECONDS):
            connection.execute("DELETE FROM video_callback_events WHERE sequence = ? AND lease_until = ?",
                               (event["sequence"], event["lease_until"]))
            if event["status"] in _FINAL_STATUSES:
                connection.execute("""
                    DELETE FROM video_task_callbacks WHERE task_id = ?
                    AND NOT EXISTS (SELECT 1 FROM video_callback_events WHERE task_id = ?)
                """, (event["task_id"], event["task_id"]))
        else:
            next_attempt = now + CALLBACK_RETRY_SECONDS[event["attempts"]]
            connection.execute("""
                UPDATE video_callback_events SET attempts = attempts + 1, due_at = ?, lease_until = 0
                WHERE sequence = ? AND lease_until = ?
            """, (next_attempt, event["sequence"], event["lease_until"]))


async def deliver_due(database: Database, send: CallbackSender = _post_callback) -> int:
    now = int(time.time())
    events = await asyncio.to_thread(_claim, database, now)

    async def deliver(event: sqlite3.Row) -> None:
        delivered = False
        with suppress(ValueError):
            headers, body = _event_payload(event, now)
            delivered = await send(event["url"], headers, body)
        await asyncio.to_thread(_finish, database, event, delivered, int(time.time()))

    await asyncio.gather(*(deliver(event) for event in events))
    return len(events)


async def run(database: Database, seconds: int) -> None:
    while True:
        await asyncio.sleep(seconds)
        try:
            await deliver_due(database)
        except Exception:
            logger.error("Video callback delivery pass failed")
