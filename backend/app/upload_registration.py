import hashlib
import logging
import re
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from posixpath import normpath
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .accounts import BusinessPrincipal
from .config import Settings
from .upload_storage import StorageError, UploadStorage


SOURCE_PREFIX = "/api/asset/source/"
SOURCE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\.[a-z0-9]{2,5}")
router = APIRouter()


@dataclass(frozen=True)
class SourceReservation:
    url: str | None = None
    asset_id: str | None = None


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS asset_upload_registrations (
            upload_id TEXT PRIMARY KEY REFERENCES asset_uploads(id),
            group_id TEXT NOT NULL REFERENCES asset_groups(id),
            name TEXT NOT NULL,
            fetch_token_hash TEXT NOT NULL UNIQUE,
            fetch_expires_at INTEGER NOT NULL,
            asset_id TEXT UNIQUE REFERENCES assets(id)
        );
    """)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_temporary_source_url(url: str | None) -> bool:
    if url is None:
        return False
    path = normpath(unquote(urlsplit(url).path))
    return path.startswith(SOURCE_PREFIX) and SOURCE_TOKEN.fullmatch(path[len(SOURCE_PREFIX):]) is not None


def reserve_source(
    connection: sqlite3.Connection, settings: Settings, storage: UploadStorage,
    principal: BusinessPrincipal, upload_id: str, *, group_id: str, name: str, media_type: str,
) -> SourceReservation:
    connection.execute("BEGIN IMMEDIATE")
    upload = connection.execute(
        "SELECT * FROM asset_uploads WHERE id = ? AND project_id = ? AND api_key_id = ?",
        (upload_id, principal.project_id, principal.key_id),
    ).fetchone()
    if upload is None:
        raise _error(404, "upload_not_found", "当前业务 Key 没有该上传记录")
    if upload["bucket"]:
        raise _error(409, "upload_storage_changed", "暂存方式已切换，请使用新的幂等键上传文件")
    if upload["asset_type"] != media_type:
        raise _error(422, "asset_media_type_mismatch", "素材类型与上传文件不一致")

    existing = connection.execute(
        "SELECT group_id, name, asset_id FROM asset_upload_registrations WHERE upload_id = ?", (upload_id,),
    ).fetchone()
    if existing is not None:
        if existing["group_id"] != group_id or existing["name"] != name:
            raise _error(409, "upload_registration_conflict", "该上传已用于其他素材登记")
        if existing["asset_id"] is not None:
            return SourceReservation(asset_id=existing["asset_id"])
        # A previous process may have stopped after Ark received the request.
        raise _error(409, "upload_registration_pending", "该上传的方舟登记结果尚未确认")

    now = int(time.time())
    if upload["expires_at"] is not None and upload["expires_at"] <= now:
        raise _error(409, "upload_expired", "上传暂存已过期，请重新上传")
    if upload["status"] != "staged":
        raise _error(409, "upload_not_staged", "上传文件尚未处于可登记状态")
    if not settings.upload_public_origin:
        raise _error(503, "upload_registration_unavailable", "尚未配置方舟可访问的 HTTPS 暂存地址")
    try:
        available = storage.path(upload["object_key"]).is_file()
    except (StorageError, OSError):
        available = False
    if not available:
        raise _error(503, "upload_storage_unavailable", "无法读取服务器暂存文件")

    suffix = PurePosixPath(upload["object_key"]).suffix
    token = f"{secrets.token_urlsafe(32)}{suffix}"
    expires_at = now + settings.upload_link_ttl_seconds
    connection.execute(
        """
        INSERT INTO asset_upload_registrations (upload_id, group_id, name, fetch_token_hash, fetch_expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (upload_id, group_id, name, _digest(token), expires_at),
    )
    connection.execute(
        "UPDATE asset_uploads SET cleanup_after = MAX(expires_at, ?) WHERE id = ?",
        (expires_at, upload_id),
    )
    return SourceReservation(url=f"{settings.upload_public_origin}{SOURCE_PREFIX}{token}")


def release_rejected_source(connection: sqlite3.Connection, upload_id: str) -> None:
    result = connection.execute(
        "DELETE FROM asset_upload_registrations WHERE upload_id = ? AND asset_id IS NULL", (upload_id,),
    )
    if result.rowcount:
        connection.execute(
            "UPDATE asset_uploads SET cleanup_after = expires_at WHERE id = ? AND status = 'staged' AND expires_at > ?",
            (upload_id, int(time.time())),
        )


def complete_source(connection: sqlite3.Connection, upload_id: str, asset_id: str) -> None:
    connection.execute(
        "UPDATE asset_upload_registrations SET asset_id = ? WHERE upload_id = ?",
        (asset_id, upload_id),
    )


class _SourceAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (isinstance(args, tuple) and len(args) == 5 and SOURCE_PREFIX in str(args[2]))


_source_access_filter = _SourceAccessFilter()


def redact_source_access_log() -> None:
    logging.getLogger("uvicorn.access").addFilter(_source_access_filter)


def _source_not_found() -> HTTPException:
    return _error(404, "asset_source_not_found", "素材下载地址无效或已过期")


@router.api_route(SOURCE_PREFIX + "{token}", methods=["GET", "HEAD"], include_in_schema=False)
def fetch_source(token: str, request: Request) -> FileResponse:
    if not SOURCE_TOKEN.fullmatch(token):
        raise _source_not_found()
    with request.app.state.db.connect() as connection:
        upload = connection.execute(
            """
            SELECT u.object_key, u.content_type, u.size_bytes FROM asset_upload_registrations AS r
            JOIN asset_uploads AS u ON u.id = r.upload_id
            JOIN project_seedance_access AS access ON access.project_id = u.project_id
            WHERE r.fetch_token_hash = ? AND r.fetch_expires_at > ? AND u.bucket = '' AND u.status = 'staged'
            """,
            (_digest(token), int(time.time())),
        ).fetchone()
    if upload is None:
        raise _source_not_found()
    try:
        path = request.app.state.upload_storage.path(upload["object_key"])
        metadata = path.stat()
    except (StorageError, OSError):
        raise _source_not_found() from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != upload["size_bytes"]:
        raise _source_not_found()
    return FileResponse(path, media_type=upload["content_type"], stat_result=metadata, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex",
    })
